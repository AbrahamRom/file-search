"""
Módulo común para la resolución de nombres de servicio mediante el DNS Service personalizado.
"""
import logging
import socket
import time
import os
from typing import Dict, Optional, Any

# Intentamos importar requests, si falla es porque falta en el entorno
try:
    import requests
except ImportError:
    requests = None

# Configuración del logger para este módulo
# Heredará la configuración del proceso padre (Server o Client)
logger = logging.getLogger(__name__)

class DNSClient:
    """
    Cliente para interactuar con el servicio DNS personalizado.
    Mantiene un caché local y realiza el bootstrap inicial.
    """
    
    def __init__(self, dns_host: str = "dns_service", dns_port: int = 5353):
        if requests is None:
            raise ImportError("La librería 'requests' es necesaria para usar DNSClient. Instálala con pip install requests.")
            
        self.dns_host = dns_host
        self.dns_port = dns_port
        self.dns_ip: Optional[str] = None
        self._cache: Dict[str, Dict[str, Any]] = {}
        
        # Intentar localizar el DNS service al iniciar
        self._bootstrap()

    def _bootstrap(self) -> None:
        """
        Localiza la IP del contenedor DNS usando el resolver nativo de Docker.
        """
        try:
            logger.info(f"Bootstrapping: Buscando IP para servicio DNS '{self.dns_host}'...")
            # Esta llamada usa el /etc/resolv.conf del contenedor (Docker DNS)
            self.dns_ip = socket.gethostbyname(self.dns_host)
            logger.info(f"DNS Service localizado en: {self.dns_ip}")
        except socket.gaierror as e:
            logger.error(f"Fallo crítico en bootstrap: No se puede resolver '{self.dns_host}'. Error: {e}")
            self.dns_ip = None

    def resolve(self, hostname: str) -> str:
        """
        Obtiene la IP para un hostname dado.
        
        Flujo:
        1. Revisa caché local (TTL).
        2. Si falla, consulta al DNS Service vía API.
        3. Si el DNS Service falla, hace fallback a resolución nativa.
        """
        # 1. Verificar Caché
        cached = self._cache.get(hostname)
        if cached:
            if time.time() < cached['expires_at']:
                logger.debug(f"Cache HIT: {hostname} -> {cached['ip']}")
                return cached['ip']
            else:
                logger.debug(f"Cache EXPIRED: {hostname}")
                del self._cache[hostname]

        # 2. Consultar API del DNS Service
        if self.dns_ip:
            try:
                url = f"http://{self.dns_ip}:{self.dns_port}/resolve/{hostname}"
                logger.debug(f"Consultando DNS API: {url}")
                
                response = requests.get(url, timeout=2.0)
                response.raise_for_status()
                
                data = response.json()
                ip = data['ip']
                ttl = data.get('ttl', 300)
                
                # Guardar en caché
                self._cache[hostname] = {
                    'ip': ip,
                    'expires_at': time.time() + ttl
                }
                logger.info(f"Resolución remota OK: {hostname} -> {ip} (TTL={ttl}s)")
                return ip
                
            except requests.RequestException as e:
                logger.warning(f"Error contactando DNS Service: {e}. Usando fallback.")
        else:
            logger.warning("DNS Service IP no disponible. Intentando re-bootstrap...")
            self._bootstrap()
            # Si sigue sin estar disponible, pasamos al fallback

        # 3. Fallback: Resolución nativa (Docker DNS)
        # Esto asegura que el servicio no se rompa si el contenedor DNS cae
        try:
            logger.info(f"Fallback: Resolviendo '{hostname}' nativamente.")
            ip = socket.gethostbyname(hostname)
            # Opcional: cachear el resultado del fallback por menos tiempo
            self._cache[hostname] = {
                'ip': ip,
                'expires_at': time.time() + 60 # 1 minuto para fallback
            }
            return ip
        except socket.gaierror:
            logger.error(f"Imposible resolver '{hostname}' incluso con fallback.")
            raise