"""
Node Manager for Storage Node.
Handles registration with DNS, heartbeats, and role management (PRIMARY/BACKUP).
"""

import asyncio
import logging
import os
import socket
import time
from typing import Optional, Dict, Callable
from datetime import datetime

import httpx

logger = logging.getLogger(__name__)

# Configuration
DNS_ALIAS = os.getenv("DNS_ALIAS", "dns")
DNS_PORT = int(os.getenv("DNS_SERVICE_PORT", 5353))
SERVER_ID = os.getenv("STORAGE_ID", os.getenv("SERVER_ID", f"storage_{socket.gethostname()}"))
SERVER_PORT = int(os.getenv("STORAGE_PORT", os.getenv("SERVER_PORT", 8000)))
HEARTBEAT_INTERVAL = int(os.getenv("HEARTBEAT_INTERVAL", 10))
DNS_RETRY_INTERVAL = int(os.getenv("DNS_RETRY_INTERVAL", 3))
DNS_MAX_RETRIES = int(os.getenv("DNS_MAX_RETRIES", 10))


class NodeManager:
    """
    Manages the lifecycle of a Storage Node in the cluster:
    - Initial registration with DNS
    - Periodic heartbeats
    - Role change handling (PRIMARY <-> BACKUP)
    """
    
    def __init__(self):
        self.server_id = SERVER_ID
        self.server_port = SERVER_PORT
        self.dns_alias = DNS_ALIAS
        self.dns_port = DNS_PORT
        
        self._role: str = "UNKNOWN"
        self._primary_info: Optional[Dict] = None
        self._my_ip: Optional[str] = None
        self._dns_url: Optional[str] = None
        self._registered: bool = False
        self._running: bool = False
        self._primary_epoch: Optional[int] = None
        self._lease_expires_at: Optional[float] = None
        
        # Callbacks for role changes
        self._on_become_primary: Optional[Callable] = None
        self._on_become_backup: Optional[Callable] = None
        
        logger.info(f"[NodeManager] Initialized: server_id={self.server_id}, port={self.server_port}")
    
    @property
    def role(self) -> str:
        return self._role
    
    @property
    def is_primary(self) -> bool:
        return self._role == "PRIMARY"
    
    @property
    def is_backup(self) -> bool:
        return self._role == "BACKUP"
    
    @property
    def primary_info(self) -> Optional[Dict]:
        """Info of the current PRIMARY (only relevant if we're BACKUP)."""
        return self._primary_info
    
    @property
    def primary_url(self) -> Optional[str]:
        """URL of the current PRIMARY."""
        if self._primary_info:
            return self._primary_info.get("url")
        return None
    
    @property
    def primary_epoch(self) -> Optional[int]:
        """Current primary epoch."""
        return self._primary_epoch
    
    def has_valid_lease(self) -> bool:
        """Check if current PRIMARY lease is still valid."""
        if not self.is_primary or self._lease_expires_at is None:
            return False
        return time.time() < self._lease_expires_at
    
    def set_callbacks(
        self, 
        on_become_primary: Optional[Callable] = None,
        on_become_backup: Optional[Callable] = None,
    ):
        """Configure callbacks for role changes."""
        self._on_become_primary = on_become_primary
        self._on_become_backup = on_become_backup
    
    def _get_my_ip(self) -> Optional[str]:
        """
        Get this container's hostname or IP address.
        
        In Docker overlay networks, we use the container hostname (e.g., storage_1)
        instead of IP addresses because:
        1. Docker's internal DNS resolves hostnames within the overlay network
        2. IP detection methods often return the wrong interface's IP
        3. Hostnames are stable and consistent across container restarts
        """
        # First, try to use the hostname directly (works best in Docker overlay networks)
        hostname = socket.gethostname()
        
        # If hostname looks like a container name (not a random hex), use it
        # Docker container hostnames are typically the container name or container ID
        if hostname and not hostname.startswith("localhost"):
            # Verify we can resolve this hostname (it should work in Docker networks)
            try:
                socket.gethostbyname(hostname)
                return hostname
            except socket.gaierror:
                pass  # Fall through to IP detection
        
        # Fallback: try to get IP address
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            try:
                return socket.gethostbyname(hostname)
            except Exception:
                return hostname if hostname else None
    
    def _discover_dns(self) -> Optional[str]:
        """Backward-compatible: retorna una URL candidata (no valida salud)."""
        urls = self._discover_dns_urls()
        return urls[0] if urls else None

    def _discover_dns_urls(self) -> list[str]:
        """Descubre todas las URLs de DNS (alias Docker puede devolver varias IPs)."""
        try:
            results = socket.getaddrinfo(
                self.dns_alias,
                self.dns_port,
                socket.AF_INET,
                socket.SOCK_STREAM,
            )
            ips = sorted(set(result[4][0] for result in results))
            return [f"http://{ip}:{self.dns_port}" for ip in ips]
        except socket.gaierror as e:
            logger.warning(f"[NodeManager] Could not resolve DNS alias '{self.dns_alias}': {e}")
        except Exception as e:
            logger.error(f"[NodeManager] Error discovering DNS: {e}")
        return []

    async def _select_healthy_dns(self, *, timeout: float = 2.0) -> Optional[str]:
        """Elige el primer DNS que responda /health."""
        urls = self._discover_dns_urls()
        if not urls:
            return None

        async with httpx.AsyncClient(timeout=timeout) as client:
            for url in urls:
                try:
                    resp = await client.get(f"{url}/health")
                    if resp.status_code == 200:
                        logger.info(f"[NodeManager] DNS available at {url} (of {len(urls)} discovered)")
                        return url
                except Exception:
                    continue
        return None
    
    async def wait_for_dns(self) -> bool:
        """
        Wait until the DNS service is available.
        Retries up to DNS_MAX_RETRIES times.
        """
        logger.info(f"[NodeManager] Waiting for DNS to be available...")
        
        for attempt in range(1, DNS_MAX_RETRIES + 1):
            dns_url = await self._select_healthy_dns(timeout=2.0)
            if dns_url:
                self._dns_url = dns_url
                logger.info(f"[NodeManager] DNS selected: {dns_url} (attempt {attempt})")
                return True
            
            logger.info(f"[NodeManager] DNS not available, retrying in {DNS_RETRY_INTERVAL}s... ({attempt}/{DNS_MAX_RETRIES})")
            await asyncio.sleep(DNS_RETRY_INTERVAL)
        
        logger.error(f"[NodeManager] DNS not available after {DNS_MAX_RETRIES} attempts")
        return False
    
    async def register(self) -> bool:
        """
        Register this server with the DNS and get an assigned role.
        """
        if not self._dns_url:
            if not await self.wait_for_dns():
                return False
        
        self._my_ip = self._get_my_ip()
        if not self._my_ip:
            logger.error("[NodeManager] Could not get server IP")
            return False
        
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(
                    f"{self._dns_url}/server/register",
                    json={
                        "server_id": self.server_id,
                        "ip": self._my_ip,
                        "port": self.server_port,
                    },
                )
                
                if response.status_code == 200:
                    data = response.json()
                    old_role = self._role
                    self._role = data.get("assigned_role", "UNKNOWN")
                    self._primary_info = data.get("primary_info")
                    self._registered = True
                    
                    # Procesar epoch y lease si somos PRIMARY
                    if self._role == "PRIMARY":
                        self._primary_epoch = data.get("primary_epoch")
                        lease_expires_str = data.get("lease_expires_at")
                        if lease_expires_str:
                            try:
                                lease_dt = datetime.fromisoformat(lease_expires_str)
                                self._lease_expires_at = lease_dt.timestamp()
                                logger.info(f"[NodeManager] Lease expires at: {lease_expires_str}")
                            except Exception as e:
                                logger.warning(f"[NodeManager] Could not parse lease_expires_at: {e}")
                    else:
                        # Si somos BACKUP, limpiar lease
                        self._primary_epoch = None
                        self._lease_expires_at = None
                    
                    logger.info(f"[NodeManager] *** REGISTERED as {self._role} ***")
                    logger.info(f"[NodeManager] Server ID: {self.server_id}, IP: {self._my_ip}")
                    
                    if self._primary_info:
                        logger.info(f"[NodeManager] Current PRIMARY: {self._primary_info}")
                    
                    # Notify role change
                    if old_role != self._role:
                        self._notify_role_change(old_role)
                    
                    return True
                else:
                    logger.error(f"[NodeManager] Registration error: {response.status_code} - {response.text}")
                    
        except Exception as e:
            logger.error(f"[NodeManager] Error registering with DNS: {e}")
        
        return False
    
    async def send_heartbeat(self) -> bool:
        """
        Send a heartbeat to the DNS and process the response.
        """
        if not self._registered or not self._dns_url:
            return False
        
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.post(
                    f"{self._dns_url}/server/heartbeat",
                    json={
                        "server_id": self.server_id,
                        "current_role": self._role,
                    },
                )
                
                if response.status_code == 200:
                    data = response.json()
                    new_role = data.get("assigned_role", self._role)
                    new_primary_info = data.get("primary_info")
                    
                    # Detect role change
                    if new_role != self._role:
                        old_role = self._role
                        self._role = new_role
                        logger.warning(f"[NodeManager] *** ROLE CHANGE: {old_role} -> {new_role} ***")
                        self._notify_role_change(old_role)
                    
                    # Actualizar epoch y lease si somos PRIMARY
                    if self._role == "PRIMARY":
                        # Obtener epoch y lease de la respuesta del heartbeat
                        new_epoch = data.get("primary_epoch")
                        if new_epoch is not None:
                            self._primary_epoch = new_epoch
                        
                        lease_expires_str = data.get("lease_expires_at")
                        if lease_expires_str:
                            try:
                                lease_dt = datetime.fromisoformat(lease_expires_str)
                                self._lease_expires_at = lease_dt.timestamp()
                                logger.debug(f"[NodeManager] Lease renewed: {lease_expires_str}")
                            except Exception as e:
                                logger.warning(f"[NodeManager] Could not parse lease_expires_at: {e}")
                    else:
                        # Si somos BACKUP, limpiar lease
                        self._primary_epoch = None
                        self._lease_expires_at = None
                    
                    # Update PRIMARY info
                    if new_primary_info != self._primary_info:
                        self._primary_info = new_primary_info
                        if self._primary_info:
                            logger.info(f"[NodeManager] PRIMARY updated: {self._primary_info}")
                    
                    return True
                elif response.status_code == 404:
                    # Not registered, re-register
                    logger.warning("[NodeManager] Not registered in DNS, re-registering...")
                    self._registered = False
                    return await self.register()
                    
        except httpx.TimeoutException:
            logger.warning("[NodeManager] Heartbeat timeout")
            self._dns_url = None
            await self._try_failover_dns()
        except Exception as e:
            logger.error(f"[NodeManager] Heartbeat error: {e}")
            self._dns_url = None
            await self._try_failover_dns()
        
        return False

    async def _try_failover_dns(self) -> bool:
        """Intenta rápidamente elegir otro DNS saludable sin bloquear demasiado."""
        new_dns = await self._select_healthy_dns(timeout=1.0)
        if new_dns:
            self._dns_url = new_dns
            logger.warning(f"[NodeManager] DNS failover -> {new_dns}")
            return True
        return False
    
    def _notify_role_change(self, old_role: str):
        """Notify role changes via callbacks."""
        if self._role == "PRIMARY" and self._on_become_primary:
            try:
                self._on_become_primary()
            except Exception as e:
                logger.error(f"[NodeManager] Error in on_become_primary callback: {e}")
        
        elif self._role == "BACKUP" and self._on_become_backup:
            try:
                self._on_become_backup()
            except Exception as e:
                logger.error(f"[NodeManager] Error in on_become_backup callback: {e}")

    def apply_dns_assignment(
        self,
        assigned_role: str,
        primary_info: Optional[Dict] = None,
        primary_epoch: Optional[int] = None,
        lease_expires_at: Optional[str] = None,
    ) -> None:
        """
        Apply a role assignment coming from the DNS service.
        This centralizes the logic of updating internal state based on DNS decisions
        and triggers callbacks if the role changed.
        """
        old_role = self._role

        # Normalize role values
        assigned_role = assigned_role.upper() if assigned_role else "UNKNOWN"

        self._role = assigned_role
        self._primary_info = primary_info

        if assigned_role == "PRIMARY":
            # Update epoch and lease expiry if provided
            if primary_epoch is not None:
                self._primary_epoch = primary_epoch
            if lease_expires_at:
                try:
                    dt = datetime.fromisoformat(lease_expires_at)
                    self._lease_expires_at = dt.timestamp()
                except Exception:
                    # ignore parse errors and keep previous lease
                    logger.warning("[NodeManager] Could not parse lease_expires_at from DNS assignment")
        else:
            # If not primary, clear epoch and lease
            self._primary_epoch = None
            self._lease_expires_at = None

        # Trigger callback if role changed
        if old_role != self._role:
            logger.info(f"[NodeManager] Role changed via DNS: {old_role} -> {self._role}")
            self._notify_role_change(old_role)
    
    async def heartbeat_loop(self):
        """
        Infinite loop that sends heartbeats periodically.
        """
        self._running = True
        logger.info(f"[NodeManager] Starting heartbeat loop (interval: {HEARTBEAT_INTERVAL}s)")
        
        while self._running:
            try:
                await self.send_heartbeat()
            except Exception as e:
                logger.error(f"[NodeManager] Error in heartbeat loop: {e}")
            
            await asyncio.sleep(HEARTBEAT_INTERVAL)
    
    def stop(self):
        """Stop the heartbeat loop."""
        self._running = False
        logger.info("[NodeManager] Stopped")
    
    def get_status(self) -> Dict:
        """Return current node status."""
        return {
            "server_id": self.server_id,
            "ip": self._my_ip,
            "port": self.server_port,
            "role": self._role,
            "registered": self._registered,
            "dns_url": self._dns_url,
            "primary_info": self._primary_info,
            "is_primary": self.is_primary,
            "is_backup": self.is_backup,
        }


# Global NodeManager instance
_node_manager: Optional[NodeManager] = None


def get_node_manager() -> NodeManager:
    """Get the global NodeManager instance."""
    global _node_manager
    if _node_manager is None:
        _node_manager = NodeManager()
    return _node_manager


__all__ = ["NodeManager", "get_node_manager"]
