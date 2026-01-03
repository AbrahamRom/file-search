"""
Módulo común para la resolución de nombres de servicio mediante el DNS Service HA.
Implementa descubrimiento dinámico y failover automático entre múltiples servidores DNS.
"""
import logging
import socket
import time
import os
import threading
import json
import random
from pathlib import Path
from typing import Dict, Optional, Any, List

from .ip_discovery import DiscoveryManager

try:
    import requests
except ImportError:
    requests = None

logger = logging.getLogger(__name__)


class DNSClientHA:
    """
    Cliente DNS con Alta Disponibilidad y Descubrimiento Dinámico.
    
    - Usa alias DNS de Docker para descubrir servidores
    - Obtiene lista de servidores DNS dinámicamente
    - Failover automático entre servidores
    - Cache local con TTL
    - Actualización periódica de lista de servidores
    """
    
    def __init__(
        self, 
        dns_alias: str = "dns",
        dns_port: int = 5353
    ):
        if requests is None:
            raise ImportError("La librería 'requests' es necesaria. Instálala con: pip install requests")
        
        self.dns_alias = dns_alias
        self.dns_port = dns_port
        
        # Lista de servidores DNS descubiertos
        # [{ip, url, server_id, role, healthy, primary_since}, ...]
        self._dns_servers: List[Dict] = []
        
        # URL del primario actual
        self._primary_url: Optional[str] = None
        
        # Cache de resoluciones
        self._cache: Dict[str, Dict[str, Any]] = {}
        
        # Cache persistente
        self._cache_dir = Path("/tmp/dns-cache")
        self._cache_file = self._cache_dir / "dns_cache.json"
        self._ensure_cache_dir()
        self._load_persistent_cache()
        
        # Discovery por rango de IPs (fallback)
        self._ip_discovery = DiscoveryManager(dns_port=dns_port)
        self._network_hint = os.getenv("NETWORK_CIDR", None)
        
        # Lock para thread-safety
        self._lock = threading.Lock()
        
        # Estado del cliente
        self._bootstrapped = False
        self._last_server_refresh = 0
        self._server_refresh_interval = 30  # Refrescar lista cada 30 segundos
        
        # Bootstrap inicial
        self._bootstrap()
    
    def _ensure_cache_dir(self) -> None:
        """Crea el directorio de caché si no existe"""
        try:
            self._cache_dir.mkdir(parents=True, exist_ok=True)
            logger.debug(f"[DNSClientHA] Directorio de caché asegurado: {self._cache_dir}")
        except Exception as e:
            logger.warning(f"[DNSClientHA] No se pudo crear directorio de caché: {e}")
    
    def _load_persistent_cache(self) -> None:
        """Carga el caché persistente del disco al iniciar"""
        try:
            if self._cache_file.exists():
                with open(self._cache_file, 'r') as f:
                    data = json.load(f)
                    self._cache = data.get("hostname_cache", {})
                    logger.info(f"[DNSClientHA] Caché persistente cargado: {len(self._cache)} entradas")
        except Exception as e:
            logger.warning(f"[DNSClientHA] No se pudo cargar caché persistente: {e}")
    
    def _save_persistent_cache(self) -> None:
        """Guarda el caché actual al disco"""
        try:
            with self._lock:
                cache_copy = dict(self._cache)
            
            cache_data = {
                "hostname_cache": cache_copy,
                "timestamp": time.time()
            }
            
            with open(self._cache_file, 'w') as f:
                json.dump(cache_data, f)
            
            logger.debug(f"[DNSClientHA] Caché persistente guardado: {len(cache_copy)} entradas")
        except Exception as e:
            logger.warning(f"[DNSClientHA] No se pudo guardar caché persistente: {e}")

    
    def _bootstrap(self) -> None:
        """
        Inicializa el cliente DNS con estrategia híbrida:
        1. Intenta descubrir via alias DNS de Docker
        2. Si falla, usa descubrimiento por rango de IPs
        """
        logger.info(f"[DNSClientHA] Iniciando bootstrap con alias '{self.dns_alias}'...")
        
        # Método 1: Descubrir IPs via alias DNS de Docker
        discovered_ips = self._discover_via_alias()
        
        if discovered_ips:
            logger.info(f"[DNSClientHA] IPs descubiertas via alias: {discovered_ips}")
            
            # Intentar obtener la lista completa de un servidor
            for ip in discovered_ips:
                if self._fetch_server_list(ip):
                    self._bootstrapped = True
                    logger.info(f"[DNSClientHA] Bootstrap exitoso. {len(self._dns_servers)} servidores conocidos")
                    return
            
            # Si no pudimos obtener la lista, usar las IPs descubiertas directamente
            logger.warning("[DNSClientHA] No se pudo obtener lista de servidores. Usando IPs descubiertas directamente.")
            with self._lock:
                self._dns_servers = [
                    {
                        "ip": ip,
                        "url": f"http://{ip}:{self.dns_port}",
                        "server_id": f"dns_{ip}",
                        "role": "unknown",
                        "healthy": True,
                        "primary_since": None
                    }
                    for ip in discovered_ips
                ]
            self._bootstrapped = True
            return
        
        # Método 2: Fallback a descubrimiento por rango de IPs
        logger.warning("[DNSClientHA] Alias DNS de Docker no disponible. Intentando descubrimiento por rango de IPs...")
        if self._bootstrap_via_ip_range():
            return
        
        # Si todo falla, quedar en estado no bootstrapeado (reintentar en resolve())
        logger.error("[DNSClientHA] Bootstrap completamente fallido. Se reintentará en las resoluciones.")
        self._bootstrapped = False
        
    
    def _bootstrap_via_ip_range(self) -> bool:
        """
        Fallback: Descubre servidores DNS usando escaneo de rango de IPs.
        Se ejecuta cuando Docker DNS alias falla.
        """
        logger.warning("[DNSClientHA] Fallback a descubrimiento por rango de IPs...")
        
        try:
            discovered = self._ip_discovery.discover_dns_servers(
                network_hint=self._network_hint,
                force_refresh=True,
                use_ping_sweep=False  # Escaneo rápido sin ping sweep
            )
            
            if not discovered:
                logger.warning("[DNSClientHA] No se encontraron servidores DNS por escaneo de IPs")
                return False
            
            logger.info(f"[DNSClientHA] Descubiertos {len(discovered)} servidores DNS por escaneo de IPs")
            
            # Procesar resultados del descubrimiento
            new_servers = []
            for srv in discovered:
                ip = srv.get("ip", "")
                server_id = srv.get("server_id", f"dns_{ip}")
                
                server_entry = {
                    "ip": ip,
                    "url": f"http://{ip}:{self.dns_port}",
                    "server_id": server_id,
                    "role": srv.get("role", "unknown"),
                    "healthy": True,
                    "primary_since": None
                }
                new_servers.append(server_entry)
                
                # Intentar obtener lista completa desde este servidor
                if self._fetch_server_list(ip):
                    with self._lock:
                        self._dns_servers = new_servers
                    self._bootstrapped = True
                    logger.info(f"[DNSClientHA] Bootstrap via IP range exitoso")
                    return True
            
            # Si no pudimos obtener lista completa, usar los descubiertos directamente
            with self._lock:
                self._dns_servers = new_servers
            self._bootstrapped = True
            logger.info(f"[DNSClientHA] Bootstrap via IP range completado (sin obtener lista completa)")
            return True
            
        except Exception as e:
            logger.error(f"[DNSClientHA] Error en descubrimiento por rango de IPs: {e}")
            return False
    
    def _attempt_rebootstrap(self) -> bool:
        """
        Intenta re-bootstrapear el cliente DNS después de una falla.
        Útil para recuperarse de particiones de red.
        Preserva el caché persistente.
        
        Orden de intento:
        1. Alias DNS de Docker
        2. Descubrimiento por rango de IPs
        3. Usar servidores conocidos previos
        """
        logger.warning("[DNSClientHA] Intentando re-bootstrap después de falla...")
        
        # Guardar caché antes de limpiar
        self._save_persistent_cache()
        
        # Limpiar estado anterior
        with self._lock:
            self._dns_servers = []
            self._primary_url = None
            # NO limpiamos self._cache para preservar el caché persistente
            self._bootstrapped = False
        
        # Intento 1: Re-ejecutar bootstrap normal (Docker DNS)
        self._bootstrap()
        
        if self._bootstrapped:
            return True
        
        # Intento 2: Fallback a descubrimiento por rango de IPs
        logger.info("[DNSClientHA] Docker DNS no disponible, intentando fallback a IP range discovery...")
        return self._bootstrap_via_ip_range()
    
    def _discover_via_alias(self) -> List[str]:
        """
        Descubre servidores DNS usando el alias compartido de Docker DNS.
        Docker DNS retorna todas las IPs que comparten el alias.
        """
        try:
            results = socket.getaddrinfo(self.dns_alias, self.dns_port, socket.AF_INET, socket.SOCK_STREAM)
            ips = list(set(result[4][0] for result in results))
            return ips
        except socket.gaierror as e:
            logger.warning(f"[DNSClientHA] No se pudo resolver alias '{self.dns_alias}': {e}")
            return []
        except Exception as e:
            logger.error(f"[DNSClientHA] Error descubriendo servidores: {e}")
            return []
    
    def _fetch_server_list(self, ip: str) -> bool:
        """
        Obtiene la lista completa de servidores DNS desde un servidor específico.
        Valida que las IPs sean alcanzables antes de agregarlas.
        """
        try:
            url = f"http://{ip}:{self.dns_port}/dns-servers"
            logger.debug(f"[DNSClientHA] Obteniendo lista de servidores desde {ip}...")
            
            response = requests.get(url, timeout=3.0)
            response.raise_for_status()
            
            data = response.json()
            all_servers = data.get("all_servers", [])
            
            if not all_servers:
                return False
            
            # Obtener IPs descubiertas localmente via Docker DNS (estas son las correctas)
            local_ips = set(self._discover_via_alias())
            
            # Procesar lista de servidores, validando IPs
            new_servers = []
            primary_url = None
            
            for srv in all_servers:
                srv_ip = srv.get("ip", "")
                srv_url = srv.get("url", "")
                
                # Si la IP del servidor está en las IPs descubiertas localmente, es válida
                # Si no está, podría ser una IP de otra red (bridge vs overlay)
                is_locally_reachable = srv_ip in local_ips
                
                # Validar que la IP es alcanzable con un check rápido
                if not is_locally_reachable and srv_ip:
                    # Intentar un health check rápido
                    try:
                        check_url = f"http://{srv_ip}:{self.dns_port}/health"
                        check_resp = requests.get(check_url, timeout=1.0)
                        is_locally_reachable = check_resp.status_code == 200
                    except Exception:
                        logger.debug(f"[DNSClientHA] IP {srv_ip} no alcanzable, ignorando")
                        is_locally_reachable = False
                
                server_entry = {
                    "ip": srv_ip,
                    "url": srv_url,
                    "server_id": srv.get("server_id", ""),
                    "role": srv.get("role", "unknown"),
                    "healthy": srv.get("healthy", True) and is_locally_reachable,
                    "primary_since": srv.get("primary_since")
                }
                
                # Solo agregar si es alcanzable o si es el mismo servidor que consultamos
                if is_locally_reachable or srv_ip == ip:
                    new_servers.append(server_entry)
                else:
                    logger.warning(f"[DNSClientHA] Ignorando servidor {srv.get('server_id')} con IP no alcanzable: {srv_ip}")
                
                if srv.get("role") == "primary" and is_locally_reachable:
                    primary_url = srv_url
            
            # Ordenar: primario primero, luego por primary_since (más antiguo = más prioridad)
            new_servers.sort(key=lambda x: (
                0 if x["role"] == "primary" else 1,
                x["primary_since"] or float('inf')
            ))
            
            with self._lock:
                self._dns_servers = new_servers
                self._primary_url = primary_url
                self._last_server_refresh = time.time()
            
            logger.info(f"[DNSClientHA] Lista actualizada: {len(new_servers)} servidores")
            for srv in new_servers:
                logger.debug(f"  - {srv['server_id']} ({srv['ip']}): {srv['role']} healthy={srv['healthy']}")
            
            return True
            
        except Exception as e:
            logger.warning(f"[DNSClientHA] Error obteniendo lista desde {ip}: {e}")
            return False
    
    def _refresh_server_list(self) -> None:
        """
        Actualiza la lista de servidores DNS.
        Primero intenta via el primario, luego via cualquier servidor conocido,
        finalmente re-descubre via alias.
        """
        logger.debug("[DNSClientHA] Actualizando lista de servidores...")
        
        # 1. Intentar con el primario actual
        if self._primary_url:
            try:
                # Extraer IP del URL
                ip = self._primary_url.replace("http://", "").split(":")[0]
                if self._fetch_server_list(ip):
                    return
            except Exception:
                pass
        
        # 2. Intentar con servidores conocidos
        with self._lock:
            servers_copy = list(self._dns_servers)
        
        for server in servers_copy:
            if not server.get("healthy"):
                continue
            ip = server.get("ip")
            if ip and self._fetch_server_list(ip):
                return
        
        # 3. Re-descubrir via alias
        logger.info("[DNSClientHA] Re-descubriendo servidores via alias...")
        discovered_ips = self._discover_via_alias()
        
        for ip in discovered_ips:
            if self._fetch_server_list(ip):
                return
        
        logger.warning("[DNSClientHA] No se pudo actualizar la lista de servidores")
    
    def _maybe_refresh_servers(self) -> None:
        """Refresca la lista de servidores si ha pasado suficiente tiempo"""
        if time.time() - self._last_server_refresh > self._server_refresh_interval:
            self._refresh_server_list()
    
    def _resolve_from_server(self, server: dict) -> Optional[str]:
        """Helper interno - no usar directamente"""
        pass  # Se implementa en resolve()
    
    def resolve(self, hostname: str) -> str:
        """
        Resuelve un hostname a IP con failover automático.
        
        Flujo:
        1. Verificar cache local (TTL)
        2. Verificar si necesitamos re-bootstrap
        3. Intentar con el primario primero
        4. Intentar con cada servidor DNS en orden
        5. Si todos fallan, intentar re-bootstrap y reintentar
        6. Usar fallback a Docker DNS nativo
        
        Args:
            hostname: Nombre del host a resolver
            
        Returns:
            IP del hostname
            
        Raises:
            socket.gaierror: Si no se puede resolver el hostname
        """
        # 1. Verificar cache
        with self._lock:
            cached = self._cache.get(hostname)
            if cached and time.time() < cached["expires_at"]:
                logger.debug(f"[DNSClientHA] Cache HIT: {hostname} -> {cached['ip']}")
                return cached["ip"]
            elif cached:
                logger.debug(f"[DNSClientHA] Cache EXPIRED: {hostname}")
                del self._cache[hostname]
        
        # 2. Verificar si necesitamos re-bootstrap
        if not self._bootstrapped:
            logger.warning("[DNSClientHA] Cliente no bootstrapeado, intentando bootstrap...")
            self._bootstrap()
        
        # Refrescar lista de servidores si es necesario
        self._maybe_refresh_servers()
        
        # 3. Intentar con el primario primero
        if self._primary_url:
            ip = self._try_resolve(self._primary_url, hostname)
            if ip:
                return ip
        
        # 4. Intentar con cada servidor DNS en orden
        with self._lock:
            servers_copy = list(self._dns_servers)
        
        for server in servers_copy:
            if not server.get("healthy"):
                continue
            
            url = server.get("url")
            if url == self._primary_url:
                continue  # Ya lo intentamos
                
            ip = self._try_resolve(url, hostname)
            if ip:
                return ip
        
        # Reintentar con servidores no healthy
        logger.warning("[DNSClientHA] Todos los servidores healthy fallaron, reintentando con los demás...")
        for server in servers_copy:
            if server.get("healthy"):
                continue
            
            url = server.get("url")
            ip = self._try_resolve(url, hostname)
            if ip:
                # Marcar como healthy de nuevo
                server["healthy"] = True
                return ip
        
        # 5. Intentar re-bootstrap si todos los servidores fallaron
        logger.warning("[DNSClientHA] Todos los servidores fallaron. Intentando re-bootstrap...")
        if self._attempt_rebootstrap():
            logger.info("[DNSClientHA] Re-bootstrap exitoso, reintentando resolución...")
            
            # Reintentar con el primario
            if self._primary_url:
                ip = self._try_resolve(self._primary_url, hostname)
                if ip:
                    return ip
            
            # Reintentar con servidores actualizados
            with self._lock:
                servers_copy = list(self._dns_servers)
            
            for server in servers_copy:
                if not server.get("healthy"):
                    continue
                ip = self._try_resolve(server.get("url"), hostname)
                if ip:
                    return ip
        
        # 6. Fallback: Cache persistente (aunque esté expirado)
        with self._lock:
            cached = self._cache.get(hostname)
            if cached:
                logger.warning(f"[DNSClientHA] Usando caché persistente expirado como último recurso: {hostname} -> {cached['ip']}")
                return cached["ip"]
        
        # 7. Fallback: Docker DNS nativo
        logger.warning(f"[DNSClientHA] Todos los servidores DNS fallaron. Usando fallback nativo para '{hostname}'")
        try:
            ip = socket.gethostbyname(hostname)
            
            with self._lock:
                self._cache[hostname] = {
                    "ip": ip,
                    "expires_at": time.time() + 60,
                    "resolved_by": "fallback",
                    "role": "native"
                }
            
            # Guardar en caché persistente
            self._save_persistent_cache()
            
            logger.info(f"[DNSClientHA] Fallback exitoso: {hostname} -> {ip}")
            return ip
            
        except socket.gaierror as e:
            logger.error(f"[DNSClientHA] Imposible resolver '{hostname}' incluso con fallback: {e}")
            raise
    
    def _try_resolve(self, url: str, hostname: str) -> Optional[str]:
        """
        Intenta resolver un hostname usando un servidor DNS específico.
        """
        try:
            resolve_url = f"{url}/resolve/{hostname}"
            logger.debug(f"[DNSClientHA] Consultando {url} para '{hostname}'")
            
            response = requests.get(resolve_url, timeout=2.0)
            response.raise_for_status()
            
            data = response.json()
            ip = data.get("ip")
            ttl = data.get("ttl", 300)
            server_id = data.get("server_id", "unknown")
            role = data.get("role", "unknown")
            
            if ip:
                with self._lock:
                    self._cache[hostname] = {
                        "ip": ip,
                        "expires_at": time.time() + ttl,
                        "resolved_by": server_id,
                        "role": role
                    }
                
                # Guardar caché persistente después de resolución exitosa
                self._save_persistent_cache()
                
                logger.info(f"[DNSClientHA] Resolución OK: {hostname} -> {ip} (via {server_id}/{role})")
                return ip
                
        except requests.Timeout:
            logger.warning(f"[DNSClientHA] Timeout contactando {url}")
            self._mark_server_unhealthy(url)
        except requests.RequestException as e:
            logger.warning(f"[DNSClientHA] Error con {url}: {e}")
            self._mark_server_unhealthy(url)
        except Exception as e:
            logger.error(f"[DNSClientHA] Error inesperado con {url}: {e}")
            self._mark_server_unhealthy(url)
        
        return None
    
    def _mark_server_unhealthy(self, url: str) -> None:
        """Marca un servidor como no healthy"""
        with self._lock:
            for server in self._dns_servers:
                if server.get("url") == url:
                    server["healthy"] = False
                    break
    
    def get_servers_status(self) -> List[dict]:
        """Retorna el estado actual de todos los servidores DNS conocidos"""
        with self._lock:
            return [
                {
                    "server_id": s.get("server_id"),
                    "ip": s.get("ip"),
                    "url": s.get("url"),
                    "role": s.get("role"),
                    "healthy": s.get("healthy")
                }
                for s in self._dns_servers
            ]
    
    def get_primary_url(self) -> Optional[str]:
        """Retorna la URL del servidor primario actual"""
        return self._primary_url
    
    def get_cache_stats(self) -> dict:
        """Retorna estadísticas del cache local"""
        with self._lock:
            now = time.time()
            valid = sum(1 for v in self._cache.values() if v["expires_at"] > now)
            expired = len(self._cache) - valid
            return {
                "total_entries": len(self._cache),
                "valid_entries": valid,
                "expired_entries": expired
            }
    
    def force_refresh(self) -> None:
        """Fuerza una actualización de la lista de servidores"""
        self._refresh_server_list()
    
    # =========================================================================
    # RESOLUCIÓN DE STORAGE NODES (para Processor)
    # =========================================================================
    
    def resolve_storage_server(self) -> Optional[Dict[str, Any]]:
        """
        Resuelve el Storage Node activo mediante /server/resolve del DNS.
        
        El Processor NO conoce la diferencia entre PRIMARY/BACKUP.
        Solo pregunta al DNS: "¿Dónde está el Storage Node?"
        El DNS maneja failover y promoción internamente.
        
        Returns:
            Dict con {server_id, ip, port, url, role} del Storage activo
            None si no hay Storage disponible
            
        Raises:
            Exception: Si no se puede contactar ningún servidor DNS
        """
        # Refrescar lista de servidores si es necesario
        self._maybe_refresh_servers()
        
        # 1. Verificar cache de storage
        with self._lock:
            cached = self._cache.get("__storage_server__")
            if cached and time.time() < cached["expires_at"]:
                logger.debug(f"[DNSClientHA] Storage cache HIT: {cached['data']['server_id']}")
                return cached["data"]
        
        # 2. Intentar con el primario primero
        if self._primary_url:
            result = self._try_resolve_storage(self._primary_url)
            if result:
                return result
        
        # 3. Intentar con cada servidor DNS en orden
        with self._lock:
            servers_copy = list(self._dns_servers)
        
        for server in servers_copy:
            if not server.get("healthy"):
                continue
            
            url = server.get("url")
            if url == self._primary_url:
                continue  # Ya lo intentamos
            
            result = self._try_resolve_storage(url)
            if result:
                return result
        
        # 4. Reintentar con servidores no healthy
        logger.warning("[DNSClientHA] Todos los servidores healthy fallaron para storage, reintentando...")
        for server in servers_copy:
            if server.get("healthy"):
                continue
            
            url = server.get("url")
            result = self._try_resolve_storage(url)
            if result:
                server["healthy"] = True
                return result
        
        logger.error("[DNSClientHA] No se pudo resolver Storage Node desde ningún DNS")
        return None
    
    def _try_resolve_storage(self, dns_url: str) -> Optional[Dict[str, Any]]:
        """
        Intenta resolver el Storage Node activo desde un servidor DNS específico.
        
        Args:
            dns_url: URL del servidor DNS (ej: http://dns_1:5353)
            
        Returns:
            Dict con info del Storage Node o None si falla
        """
        try:
            resolve_url = f"{dns_url}/server/resolve"
            logger.debug(f"[DNSClientHA] Consultando Storage desde {dns_url}")
            
            response = requests.get(resolve_url, timeout=3.0)
            
            if response.status_code == 503:
                # No hay storage disponible
                logger.warning(f"[DNSClientHA] DNS reporta: no hay Storage disponible")
                return None
            
            response.raise_for_status()
            data = response.json()
            
            result = {
                "server_id": data.get("server_id"),
                "ip": data.get("ip"),
                "port": data.get("port"),
                "url": data.get("url"),
                "role": data.get("role", "PRIMARY"),
            }
            
            # Guardar en cache (TTL corto para storage - 10 segundos)
            with self._lock:
                self._cache["__storage_server__"] = {
                    "data": result,
                    "expires_at": time.time() + 10,  # TTL de 10 segundos
                    "resolved_by": dns_url,
                }
            
            logger.info(
                f"[DNSClientHA] Storage resuelto: {result['server_id']} -> {result['url']} (via {dns_url})"
            )
            return result
            
        except requests.Timeout:
            logger.warning(f"[DNSClientHA] Timeout consultando storage desde {dns_url}")
            self._mark_server_unhealthy(dns_url)
        except requests.RequestException as e:
            logger.warning(f"[DNSClientHA] Error consultando storage desde {dns_url}: {e}")
            self._mark_server_unhealthy(dns_url)
        except Exception as e:
            logger.error(f"[DNSClientHA] Error inesperado consultando storage desde {dns_url}: {e}")
            self._mark_server_unhealthy(dns_url)
        
        return None
    
    def invalidate_storage_cache(self, failed_storage_id: Optional[str] = None) -> None:
        """
        Invalida el cache del Storage Node.
        
        Llamar cuando el Storage Node actual falla (circuit breaker abre).
        La siguiente llamada a resolve_storage_server() consultará al DNS
        para obtener un nuevo Storage Node (posiblemente después de failover).
        
        Args:
            failed_storage_id: ID del storage que falló (para logging)
        """
        with self._lock:
            if "__storage_server__" in self._cache:
                old = self._cache["__storage_server__"]
                old_id = old['data']['server_id']
                logger.info(
                    f"[DNSClientHA] Invalidando cache de Storage: {old_id}"
                )
                del self._cache["__storage_server__"]
                
                # Si el storage que falló es el mismo que teníamos en caché,
                # también forzar refresh de lista de DNS para buscar en otras particiones
                if failed_storage_id and failed_storage_id == old_id:
                    logger.info("[DNSClientHA] Forzando refresh de servidores DNS para buscar otras particiones")
                    self._last_server_refresh = 0  # Forzar refresh inmediato
    
    def resolve_storage_from_all_dns(self, exclude_storage_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """
        Intenta resolver Storage consultando TODOS los servidores DNS conocidos.
        
        Útil en casos de split-brain donde diferentes DNS pueden tener
        diferentes views del PRIMARY.
        
        Args:
            exclude_storage_id: Excluir este storage ID de los resultados
            
        Returns:
            Dict con info del Storage o None
        """
        # Primero refrescar lista de DNS (podría descubrir nuevos)
        self._refresh_server_list()
        
        results = []
        
        with self._lock:
            servers_copy = list(self._dns_servers)
        
        # Consultar TODOS los DNS (no solo los healthy)
        for server in servers_copy:
            url = server.get("url")
            if not url:
                continue
            
            result = self._try_resolve_storage(url)
            if result:
                # Si debemos excluir cierto storage, continuar buscando
                if exclude_storage_id and result.get("server_id") == exclude_storage_id:
                    logger.info(f"[DNSClientHA] DNS {url} reportó {exclude_storage_id}, buscando alternativa...")
                    continue
                
                # Encontramos un storage diferente o válido
                logger.info(f"[DNSClientHA] Encontrado Storage alternativo: {result['server_id']} via {url}")
                return result
        
        logger.warning(f"[DNSClientHA] No se encontró Storage alternativo (excluido: {exclude_storage_id})")
        return None
    
    def get_storage_cache_status(self) -> Dict[str, Any]:
        """Retorna el estado del cache de Storage Node."""
        with self._lock:
            cached = self._cache.get("__storage_server__")
            if cached:
                return {
                    "cached": True,
                    "server_id": cached["data"]["server_id"],
                    "url": cached["data"]["url"],
                    "expires_in": max(0, cached["expires_at"] - time.time()),
                    "resolved_by": cached.get("resolved_by"),
                }
            return {
                "cached": False,
                "server_id": None,
                "url": None,
            }
    
    def list_storage_servers(self) -> List[Dict[str, Any]]:
        """
        Lista todos los Storage Nodes registrados en el DNS.
        
        Returns:
            Lista de Storage Nodes con su estado
        """
        # Refrescar lista de servidores si es necesario
        self._maybe_refresh_servers()
        
        # Intentar con el primario primero
        if self._primary_url:
            result = self._try_list_storage(self._primary_url)
            if result is not None:
                return result
        
        # Intentar con otros servidores en orden aleatorio (balanceo simple)
        with self._lock:
            servers_copy = [s for s in self._dns_servers if s.get("healthy") and s.get("url") != self._primary_url]
        random.shuffle(servers_copy)
        
        for server in servers_copy:
            result = self._try_list_storage(server.get("url"))
            if result is not None:
                return result
        
        logger.error("[DNSClientHA] No se pudo listar Storage Nodes")
        return []
    
    def _try_list_storage(self, dns_url: str) -> Optional[List[Dict[str, Any]]]:
        """Intenta listar Storage Nodes desde un DNS específico."""
        try:
            response = requests.get(f"{dns_url}/server/list", timeout=3.0)
            response.raise_for_status()
            data = response.json()
            return data.get("servers", [])
        except Exception as e:
            logger.warning(f"[DNSClientHA] Error listando storage desde {dns_url}: {e}")
            return None


# Alias para compatibilidad con código existente
class DNSClient(DNSClientHA):
    """
    Alias para mantener compatibilidad con código que use DNSClient.
    """
    
    def __init__(self, dns_host: str = "dns", dns_port: int = 5353):
        # Obtener alias desde variable de entorno
        dns_alias = os.getenv("DNS_ALIAS", dns_host)
        
        super().__init__(
            dns_alias=dns_alias,
            dns_port=dns_port
        )
