"""
Gestión del nodo del servidor API en el clúster.
Maneja registro con DNS, heartbeats y roles (PRIMARY/BACKUP).
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

# Configuración
DNS_ALIAS = os.getenv("DNS_ALIAS", "dns")
DNS_PORT = int(os.getenv("DNS_SERVICE_PORT", 5353))
SERVER_ID = os.getenv("SERVER_ID", f"server_{socket.gethostname()}")
SERVER_PORT = int(os.getenv("SERVER_PORT", 8000))
HEARTBEAT_INTERVAL = int(os.getenv("HEARTBEAT_INTERVAL", 5))
DNS_RETRY_INTERVAL = int(os.getenv("DNS_RETRY_INTERVAL", 3))
DNS_MAX_RETRIES = int(os.getenv("DNS_MAX_RETRIES", 10))


class NodeManager:
    """
    Gestiona el ciclo de vida del nodo en el clúster:
    - Registro inicial con el DNS
    - Heartbeats periódicos
    - Manejo de cambios de rol (PRIMARY <-> BACKUP)
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
        
        # Callbacks para notificar cambios de rol
        self._on_become_primary: Optional[Callable] = None
        self._on_become_backup: Optional[Callable] = None
        
        logger.info(f"[NodeManager] Inicializado: server_id={self.server_id}, port={self.server_port}")
    
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
        """Info del PRIMARY actual (solo relevante si soy BACKUP)."""
        return self._primary_info
    
    @property
    def primary_url(self) -> Optional[str]:
        """URL del PRIMARY actual."""
        if self._primary_info:
            return self._primary_info.get("url")
        return None
    
    def set_callbacks(
        self, 
        on_become_primary: Optional[Callable] = None,
        on_become_backup: Optional[Callable] = None
    ):
        """Configura callbacks para cambios de rol."""
        self._on_become_primary = on_become_primary
        self._on_become_backup = on_become_backup
    
    def _get_my_ip(self) -> Optional[str]:
        """Obtiene la IP de este contenedor."""
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            try:
                return socket.gethostbyname(socket.gethostname())
            except Exception:
                return None
    
    def _discover_dns(self) -> Optional[str]:
        """
        Descubre un servidor DNS usando el alias de Docker DNS.
        Retorna la URL del primer servidor DNS disponible.
        """
        try:
            results = socket.getaddrinfo(
                self.dns_alias, 
                self.dns_port, 
                socket.AF_INET, 
                socket.SOCK_STREAM
            )
            ips = sorted(set(result[4][0] for result in results))  # Ordenar para consistencia
            
            if ips:
                # Usar el primero (ordenado para que todos los servidores usen el mismo)
                dns_ip = ips[0]
                url = f"http://{dns_ip}:{self.dns_port}"
                logger.info(f"[NodeManager] DNS descubierto: {url} (de {len(ips)} disponibles)")
                return url
                
        except socket.gaierror as e:
            logger.warning(f"[NodeManager] No se pudo resolver DNS alias '{self.dns_alias}': {e}")
        except Exception as e:
            logger.error(f"[NodeManager] Error descubriendo DNS: {e}")
        
        return None
    
    async def wait_for_dns(self) -> bool:
        """
        Espera hasta que el servicio DNS esté disponible.
        Reintenta hasta DNS_MAX_RETRIES veces.
        """
        logger.info(f"[NodeManager] Esperando a que DNS esté disponible...")
        
        for attempt in range(1, DNS_MAX_RETRIES + 1):
            dns_url = self._discover_dns()
            
            if dns_url:
                try:
                    async with httpx.AsyncClient(timeout=5.0) as client:
                        response = await client.get(f"{dns_url}/health")
                        if response.status_code == 200:
                            self._dns_url = dns_url
                            logger.info(f"[NodeManager] DNS disponible en {dns_url} (intento {attempt})")
                            return True
                except Exception as e:
                    logger.debug(f"[NodeManager] DNS no responde: {e}")
            
            logger.info(f"[NodeManager] DNS no disponible, reintentando en {DNS_RETRY_INTERVAL}s... ({attempt}/{DNS_MAX_RETRIES})")
            await asyncio.sleep(DNS_RETRY_INTERVAL)
        
        logger.error(f"[NodeManager] DNS no disponible después de {DNS_MAX_RETRIES} intentos")
        return False
    
    async def register(self) -> bool:
        """
        Registra este servidor con el DNS y obtiene un rol asignado.
        """
        if not self._dns_url:
            if not await self.wait_for_dns():
                return False
        
        self._my_ip = self._get_my_ip()
        if not self._my_ip:
            logger.error("[NodeManager] No se pudo obtener la IP del servidor")
            return False
        
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(
                    f"{self._dns_url}/server/register",
                    json={
                        "server_id": self.server_id,
                        "ip": self._my_ip,
                        "port": self.server_port
                    }
                )
                
                if response.status_code == 200:
                    data = response.json()
                    old_role = self._role
                    self._role = data.get("assigned_role", "UNKNOWN")
                    self._primary_info = data.get("primary_info")
                    self._registered = True
                    
                    logger.info(f"[NodeManager] *** REGISTRADO como {self._role} ***")
                    logger.info(f"[NodeManager] Server ID: {self.server_id}, IP: {self._my_ip}")
                    
                    if self._primary_info:
                        logger.info(f"[NodeManager] PRIMARY actual: {self._primary_info}")
                    
                    # Notificar cambio de rol
                    if old_role != self._role:
                        self._notify_role_change(old_role)
                    
                    return True
                else:
                    logger.error(f"[NodeManager] Error en registro: {response.status_code} - {response.text}")
                    
        except Exception as e:
            logger.error(f"[NodeManager] Error registrando con DNS: {e}")
        
        return False
    
    async def send_heartbeat(self) -> bool:
        """
        Envía un heartbeat al DNS y procesa la respuesta.
        """
        if not self._registered or not self._dns_url:
            return False
        
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.post(
                    f"{self._dns_url}/server/heartbeat",
                    json={
                        "server_id": self.server_id,
                        "current_role": self._role
                    }
                )
                
                if response.status_code == 200:
                    data = response.json()
                    new_role = data.get("assigned_role", self._role)
                    new_primary_info = data.get("primary_info")
                    
                    # Detectar cambio de rol
                    if new_role != self._role:
                        old_role = self._role
                        self._role = new_role
                        logger.warning(f"[NodeManager] *** CAMBIO DE ROL: {old_role} -> {new_role} ***")
                        self._notify_role_change(old_role)
                    
                    # Actualizar info del PRIMARY
                    if new_primary_info != self._primary_info:
                        self._primary_info = new_primary_info
                        if self._primary_info:
                            logger.info(f"[NodeManager] PRIMARY actualizado: {self._primary_info}")
                    
                    return True
                elif response.status_code == 404:
                    # No estamos registrados, re-registrar
                    logger.warning("[NodeManager] No registrado en DNS, re-registrando...")
                    self._registered = False
                    return await self.register()
                    
        except httpx.TimeoutException:
            logger.warning("[NodeManager] Timeout en heartbeat")
            # Intentar redescubrir DNS
            self._dns_url = self._discover_dns()
        except Exception as e:
            logger.error(f"[NodeManager] Error en heartbeat: {e}")
            # Intentar redescubrir DNS
            self._dns_url = self._discover_dns()
        
        return False
    
    def _notify_role_change(self, old_role: str):
        """Notifica cambios de rol via callbacks."""
        if self._role == "PRIMARY" and self._on_become_primary:
            try:
                self._on_become_primary()
            except Exception as e:
                logger.error(f"[NodeManager] Error en callback on_become_primary: {e}")
        
        elif self._role == "BACKUP" and self._on_become_backup:
            try:
                self._on_become_backup()
            except Exception as e:
                logger.error(f"[NodeManager] Error en callback on_become_backup: {e}")
    
    async def heartbeat_loop(self):
        """
        Loop infinito que envía heartbeats periódicamente.
        """
        self._running = True
        logger.info(f"[NodeManager] Iniciando heartbeat loop (intervalo: {HEARTBEAT_INTERVAL}s)")
        
        while self._running:
            try:
                await self.send_heartbeat()
            except Exception as e:
                logger.error(f"[NodeManager] Error en heartbeat loop: {e}")
            
            await asyncio.sleep(HEARTBEAT_INTERVAL)
    
    def stop(self):
        """Detiene el loop de heartbeats."""
        self._running = False
        logger.info("[NodeManager] Detenido")
    
    def get_status(self) -> Dict:
        """Retorna el estado actual del nodo."""
        return {
            "server_id": self.server_id,
            "ip": self._my_ip,
            "port": self.server_port,
            "role": self._role,
            "registered": self._registered,
            "dns_url": self._dns_url,
            "primary_info": self._primary_info,
            "is_primary": self.is_primary,
            "is_backup": self.is_backup
        }


# Instancia global del NodeManager
_node_manager: Optional[NodeManager] = None


def get_node_manager() -> NodeManager:
    """Obtiene la instancia global del NodeManager."""
    global _node_manager
    if _node_manager is None:
        _node_manager = NodeManager()
    return _node_manager
