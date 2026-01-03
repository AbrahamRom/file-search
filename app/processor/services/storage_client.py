"""
Storage Client - HTTP client for communicating with Storage Nodes via DNS.

The Processor does NOT know about PRIMARY/BACKUP Storage Nodes.
It only knows how to ask the DNS for the current active Storage Node.

Flow:
1. Client calls list_files(), search_files(), etc.
2. StorageClient asks DNSClientHA for current Storage URL via /server/resolve
3. StorageClient makes request to Storage Node
4. If request fails (circuit breaker opens), invalidate DNS cache
5. On next request, DNS will return the new active node (after failover)

This design ensures:
- Processor is truly stateless (no node list to maintain)
- DNS is the single source of truth for Storage Node availability
- Failover is transparent - DNS handles promotion of BACKUP to PRIMARY
"""

import asyncio
import logging
import os
from dataclasses import dataclass, field
from typing import Optional, Dict, Any, List
from datetime import datetime

import httpx

# Usar el DNSClientHA común con extensiones para Storage
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
from common.resolver import DNSClientHA

from .circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerOpen,
    CircuitBreakerRegistry,
    get_circuit_breaker_registry,
)

logger = logging.getLogger(__name__)

# Configuration
STORAGE_TIMEOUT = float(os.getenv("STORAGE_TIMEOUT", 10.0))
MAX_RETRIES = int(os.getenv("STORAGE_MAX_RETRIES", 3))
RETRY_DELAY = float(os.getenv("STORAGE_RETRY_DELAY", 0.5))


class StorageError(Exception):
    """Base exception for storage operations."""
    pass


class StorageUnavailableError(StorageError):
    """Raised when no Storage Nodes are available."""
    pass


class StorageRequestError(StorageError):
    """Raised when a storage request fails."""
    def __init__(self, message: str, storage_id: str = "unknown", status_code: Optional[int] = None):
        self.storage_id = storage_id
        self.status_code = status_code
        super().__init__(message)


class DNSError(StorageError):
    """Raised when DNS resolution fails."""
    pass


@dataclass
class StorageClient:
    """
    Client for communicating with Storage Nodes via DNS resolution.
    
    Key difference from previous implementation:
    - Does NOT maintain a list of nodes
    - Does NOT know about PRIMARY/BACKUP
    - Asks DNS for the active Storage Node on each request (with caching)
    - When Storage fails, invalidates DNS cache so DNS can route to new node
    
    The Circuit Breaker is keyed by storage_id (from DNS resolution).
    When it opens, we invalidate DNS cache to force re-resolution.
    """
    
    dns_client: Optional[DNSClientHA] = None
    timeout: float = STORAGE_TIMEOUT
    max_retries: int = MAX_RETRIES
    retry_delay: float = RETRY_DELAY
    
    _http_client: Optional[httpx.AsyncClient] = field(default=None, init=False)
    _current_storage_id: Optional[str] = field(default=None, init=False)
    
    async def __aenter__(self):
        """Initialize HTTP client and DNS client."""
        self._http_client = httpx.AsyncClient(timeout=self.timeout)
        
        if self.dns_client is None:
            dns_alias = os.getenv("DNS_ALIAS", "dns")
            dns_port = int(os.getenv("DNS_SERVICE_PORT", 5353))
            self.dns_client = DNSClientHA(dns_alias=dns_alias, dns_port=dns_port)
        
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Close HTTP client."""
        if self._http_client:
            await self._http_client.aclose()
            self._http_client = None
    
    async def _ensure_client(self):
        """Ensure HTTP client is initialized."""
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(timeout=self.timeout)
        if self.dns_client is None:
            dns_alias = os.getenv("DNS_ALIAS", "dns")
            dns_port = int(os.getenv("DNS_SERVICE_PORT", 5353))
            self.dns_client = DNSClientHA(dns_alias=dns_alias, dns_port=dns_port)
    
    def _get_storage_info(
        self,
        force_refresh: bool = False,
        exclude_storage_id: Optional[str] = None,
        mode: str = "read",
    ) -> tuple[str, str]:
        """
        Get current Storage Node URL from DNS.
        
        Args:
            force_refresh: Force DNS cache invalidation
            exclude_storage_id: Exclude this storage ID and search for alternatives
            mode: "read" para distribuir lecturas, "write" para forzar PRIMARY
        
        Returns:
            Tuple of (storage_url, storage_id)
        """
        if force_refresh:
            self.dns_client.invalidate_storage_cache(self._current_storage_id)
        
        # Si hay un storage a excluir (ej: devolvió lease_expired), buscar alternativa
        if exclude_storage_id:
            storage_info = self.dns_client.resolve_storage_from_all_dns(exclude_storage_id=exclude_storage_id)
        elif mode == "write":
            storage_info = self.dns_client.resolve_storage_server_for_write()
        else:
            storage_info = self.dns_client.resolve_storage_server_for_read()
        
        if not storage_info:
            raise StorageUnavailableError("No storage nodes available from DNS")
        
        self._current_storage_id = storage_info["server_id"]
        return storage_info["url"], storage_info["server_id"]
    
    async def _make_request(
        self,
        method: str,
        path: str,
        storage_url: str,
        storage_id: str,
        **kwargs,
    ) -> httpx.Response:
        """
        Make an HTTP request to a Storage Node with circuit breaker.
        
        Raises:
            CircuitBreakerOpen: If circuit breaker is open
            StorageRequestError: If request fails
        """
        registry = get_circuit_breaker_registry()
        breaker = await registry.get_breaker(storage_id)
        
        url = f"{storage_url}{path}"
        
        async with breaker:
            try:
                response = await self._http_client.request(method, url, **kwargs)
                
                # Handle fencing errors (409 Conflict = lease expired, 503 = not primary)
                if response.status_code in (409, 503):
                    try:
                        error_detail = response.json()
                        error_type = error_detail.get("detail", {}).get("error") if isinstance(error_detail.get("detail"), dict) else None
                        
                        if error_type in ("lease_expired", "not_primary"):
                            logger.warning(
                                f"[FENCING] Storage {storage_id} rejected write: {error_type}. "
                                f"Will invalidate DNS cache and retry with current PRIMARY."
                            )
                            raise StorageRequestError(
                                f"Fencing error: {error_type}",
                                storage_id,
                                response.status_code,
                            )
                    except (ValueError, KeyError):
                        pass  # Not a fencing error, treat as normal error
                
                if response.status_code >= 500:
                    raise StorageRequestError(
                        f"Server error: {response.status_code}",
                        storage_id,
                        response.status_code,
                    )
                
                return response
                
            except httpx.RequestError as e:
                raise StorageRequestError(
                    f"Request failed: {str(e)}",
                    storage_id,
                )
    
    async def _request_with_dns_failover(
        self,
        method: str,
        path: str,
        *,
        read_only: bool = True,
        **kwargs,
    ) -> httpx.Response:
        """
        Make a request with DNS-based failover.
        
        Flow:
        1. Get Storage URL from DNS (cached)
        2. Try request with circuit breaker
        3. If circuit breaker opens OR request fails repeatedly:
           a. Invalidate DNS cache
           b. Re-resolve from DNS (will get new node after failover)
           c. Retry with new node
        4. If fencing error (lease_expired/not_primary), exclude that storage and find another
        5. If still fails after all attempts, raise error
        """
        await self._ensure_client()
        
        # Track if we've already tried DNS re-resolution
        dns_refreshed = False
        last_error: Optional[Exception] = None
        failed_storage_id: Optional[str] = None  # Storage que falló por fencing
        
        for attempt in range(self.max_retries + 1):  # +1 for DNS refresh attempt
            try:
                # Get current Storage Node from DNS
                # Si hubo error de fencing, excluir ese storage y buscar alternativo
                storage_url, storage_id = self._get_storage_info(
                    force_refresh=dns_refreshed,
                    exclude_storage_id=failed_storage_id,
                    mode="read" if read_only else "write",
                )
                
                logger.debug(
                    "Request to %s: %s %s (attempt %d, dns_refreshed=%s, excluded=%s)",
                    storage_id,
                    method,
                    path,
                    attempt + 1,
                    dns_refreshed,
                    failed_storage_id,
                )
                
                response = await self._make_request(
                    method, path, storage_url, storage_id, **kwargs
                )
                return response
                
            except CircuitBreakerOpen as e:
                logger.warning(
                    "Circuit breaker open for %s: %s",
                    storage_id if 'storage_id' in dir() else "unknown",
                    str(e),
                )
                last_error = e
                
                # Circuit breaker is open - invalidate DNS cache and retry
                if not dns_refreshed:
                    logger.info(
                        "Circuit breaker open - invalidating DNS cache and re-resolving"
                    )
                    self.dns_client.invalidate_storage_cache(storage_id if 'storage_id' in dir() else None)
                    dns_refreshed = True
                    await asyncio.sleep(self.retry_delay)
                    continue
                else:
                    # Already tried DNS refresh, give up
                    break
                    
            except (StorageRequestError, DNSError) as e:
                logger.warning(
                    "Request failed (attempt %d): %s",
                    attempt + 1,
                    str(e),
                )
                last_error = e
                
                # Detectar errores de fencing y marcar el storage para exclusión
                is_fencing_error = isinstance(e, StorageRequestError) and "Fencing error" in str(e)
                
                if is_fencing_error and 'storage_id' in dir():
                    # Error de fencing - este storage no es PRIMARY válido
                    # Marcarlo para exclusión y buscar otro
                    failed_storage_id = storage_id
                    logger.info(
                        f"Fencing error from {storage_id} - will search for alternative Storage"
                    )
                    self.dns_client.invalidate_storage_cache(storage_id)
                    dns_refreshed = True
                    
                    if attempt < self.max_retries:
                        await asyncio.sleep(self.retry_delay)
                    continue
                
                # Si no es fencing y no hemos refrescado DNS, hacerlo
                if not dns_refreshed and isinstance(e, StorageRequestError):
                    logger.info(
                        "Storage request failed - invalidating DNS cache for failover"
                    )
                    self.dns_client.invalidate_storage_cache(self._current_storage_id)
                    dns_refreshed = True
                
                if attempt < self.max_retries:
                    await asyncio.sleep(self.retry_delay * (attempt + 1))
                continue
                
            except StorageUnavailableError as e:
                # DNS says no storage available - nothing we can do
                logger.error("No storage nodes available: %s", e)
                raise
                
            except Exception as e:
                logger.error("Unexpected error: %s", str(e))
                last_error = e
                break
        
        # All attempts failed
        raise StorageUnavailableError(
            f"All storage attempts failed. Last error: {last_error}"
        )
    
    # =========================================================================
    # PUBLIC API METHODS
    # =========================================================================
    
    async def list_files(self, shard_id: Optional[str] = None) -> List[Dict]:
        """List all files from Storage Node."""
        params = {}
        if shard_id:
            params["shard_id"] = shard_id
        
        response = await self._request_with_dns_failover("GET", "/files", params=params, read_only=True)
        response.raise_for_status()
        return response.json()
    
    async def get_file(self, file_id: str) -> Optional[Dict]:
        """Get file metadata by ID."""
        response = await self._request_with_dns_failover("GET", f"/files/{file_id}", read_only=True)
        
        if response.status_code == 404:
            return None
        
        response.raise_for_status()
        return response.json()
    
    async def search_files(
        self,
        query: str,
        limit: int = 10,
        offset: int = 0,
        shard_id: Optional[str] = None,
    ) -> Dict:
        """Search files by name."""
        params = {"query": query, "limit": limit, "offset": offset}
        if shard_id:
            params["shard_id"] = shard_id
        
        response = await self._request_with_dns_failover("GET", "/search", params=params, read_only=True)
        response.raise_for_status()
        return response.json()
    
    async def download_file(self, file_id: str) -> bytes:
        """
        Download file content with multi-storage fallback.
        
        If the primary storage returns 404 "File not available on disk",
        tries to download from other storage nodes (file might exist there
        after a network partition where files were uploaded to different nodes).
        """
        await self._ensure_client()
        
        # First try: normal request with DNS failover
        try:
            response = await self._request_with_dns_failover(
                "GET",
                f"/files/{file_id}/download",
                read_only=True,
            )
            
            # Check for "file not on disk" error (404 with specific message)
            if response.status_code == 404:
                try:
                    error_detail = response.json().get("detail", "")
                    if "not available on disk" in error_detail.lower():
                        logger.warning(
                            f"[DOWNLOAD] File {file_id} not on disk in primary, trying other storages..."
                        )
                        # Fall through to multi-storage fallback
                        raise StorageRequestError(
                            f"File not available on disk: {file_id}",
                            self._current_storage_id or "unknown",
                            404
                        )
                except (ValueError, AttributeError):
                    pass
                response.raise_for_status()
            
            response.raise_for_status()
            return response.content
            
        except StorageRequestError as e:
            # Only do multi-storage fallback for "not on disk" errors
            if "not available on disk" not in str(e).lower():
                raise
            
            # Multi-storage fallback: try all storage nodes
            logger.info(f"[DOWNLOAD] Attempting multi-storage fallback for {file_id}")
            
            tried_storages = {self._current_storage_id}
            all_storages = self.dns_client.list_storage_servers()
            
            for storage_info in all_storages:
                storage_id = storage_info.get("server_id")
                storage_url = storage_info.get("url")
                
                if not storage_url or storage_id in tried_storages:
                    continue
                
                tried_storages.add(storage_id)
                
                try:
                    logger.info(f"[DOWNLOAD] Trying storage {storage_id} at {storage_url}")
                    
                    download_url = f"{storage_url}/files/{file_id}/download"
                    response = await self._http_client.get(download_url)
                    
                    if response.status_code == 200:
                        logger.info(f"[DOWNLOAD] Successfully downloaded from {storage_id}")
                        return response.content
                    elif response.status_code == 404:
                        logger.debug(f"[DOWNLOAD] File not found on {storage_id}")
                        continue
                    else:
                        logger.warning(f"[DOWNLOAD] Error from {storage_id}: {response.status_code}")
                        continue
                        
                except Exception as fallback_error:
                    logger.warning(f"[DOWNLOAD] Failed to reach {storage_id}: {fallback_error}")
                    continue
            
            # All storages tried, file not found anywhere
            raise StorageRequestError(
                f"File {file_id} not available on disk in any storage node",
                "all",
                404
            )
    
    async def upload_file(
        self,
        filename: str,
        content: bytes,
        folder: Optional[str] = None,
        shard_id: Optional[str] = None,
    ) -> Dict:
        """Upload a file to Storage Node."""
        files = {"file": (filename, content)}
        data = {}
        if folder:
            data["folder"] = folder
        if shard_id:
            data["shard_id"] = shard_id
        
        response = await self._request_with_dns_failover(
            "POST",
            "/upload",
            files=files,
            data=data,
            read_only=False,
        )
        response.raise_for_status()
        return response.json()
    
    async def delete_file(self, file_id: str) -> bool:
        """Delete a file."""
        response = await self._request_with_dns_failover("DELETE", f"/files/{file_id}", read_only=False)
        return response.status_code == 200
    
    async def upsert_file(self, file_data: Dict) -> Dict:
        """Upsert file metadata."""
        response = await self._request_with_dns_failover(
            "POST",
            "/files",
            json=file_data,
            read_only=False,
        )
        response.raise_for_status()
        return response.json()
    
    async def get_storage_status(self) -> Dict:
        """Get status of the current Storage Node."""
        response = await self._request_with_dns_failover("GET", "/status", read_only=True)
        response.raise_for_status()
        return response.json()
    
    async def check_health(self) -> Dict[str, Any]:
        """
        Check health of current Storage Node.
        
        Returns dict with:
        - storage_id: Current storage node ID
        - storage_url: Current storage node URL
        - healthy: Whether node responded
        - dns_cache: DNS cache status
        """
        await self._ensure_client()
        
        try:
            storage_url, storage_id = self._get_storage_info()
            
            response = await self._http_client.get(f"{storage_url}/health")
            healthy = response.status_code == 200
            
            return {
                "storage_id": storage_id,
                "storage_url": storage_url,
                "healthy": healthy,
                "dns_cache": self.dns_client.get_storage_cache_status(),
            }
            
        except (DNSError, StorageUnavailableError) as e:
            return {
                "storage_id": None,
                "storage_url": None,
                "healthy": False,
                "error": str(e),
                "dns_cache": self.dns_client.get_storage_cache_status(),
            }
    
    def get_current_storage_id(self) -> Optional[str]:
        """Get the currently resolved storage ID (from last request)."""
        return self._current_storage_id
    
    def get_all_storage_nodes(self) -> List[Dict]:
        """Get list of all storage nodes from DNS (for monitoring)."""
        return self.dns_client.list_storage_servers()


# Global client instance
_client: Optional[StorageClient] = None


def get_storage_client() -> StorageClient:
    """Get the global storage client instance."""
    global _client
    if _client is None:
        _client = StorageClient()
    return _client


async def init_storage_client(
    dns_client: Optional[DNSClientHA] = None,
) -> StorageClient:
    """
    Initialize the storage client with DNS client.
    
    Args:
        dns_client: Optional DNS client instance (creates new if not provided)
    """
    global _client
    _client = StorageClient(dns_client=dns_client)
    await _client.__aenter__()
    return _client


__all__ = [
    "StorageClient",
    "StorageError",
    "StorageUnavailableError",
    "StorageRequestError",
    "get_storage_client",
    "init_storage_client",
]
