"""
Módulo para descubrimiento de servicios DNS mediante escaneo de rango de IPs.
Proporciona descubrimiento automático de nodos cuando Docker DNS falla.

Estrategias soportadas (sin requerir configuración especial de Docker):
1. Detección automática de red local
2. Escaneo rápido de puertos conocidos (5353, 8000)
3. Ping sweep para identificar hosts activos
4. Identificación de tipo de servicio mediante health checks HTTP
"""

import socket
import logging
import time
import subprocess
import re
from typing import List, Dict, Optional, Tuple, Set
from concurrent.futures import ThreadPoolExecutor, as_completed
from ipaddress import IPv4Network, IPv4Address
import threading

try:
    import requests
except ImportError:
    requests = None

logger = logging.getLogger(__name__)


class IPRangeDiscovery:
    """
    Descubre servicios en una red local mediante escaneo de puertos.
    
    Características:
    - Auto-detección de red local
    - Escaneo paralelo de puertos TCP
    - Identificación de servicios mediante health checks HTTP
    - Sin requisitos especiales (solo socket y subprocess)
    """
    
    def __init__(self, timeout: float = 0.5, max_workers: int = 20):
        """
        Args:
            timeout: Timeout para conexiones TCP (segundos)
            max_workers: Número máximo de threads paralelos
        """
        self.timeout = timeout
        self.max_workers = max_workers
        self.dns_port = 5353
        self.storage_port = 8000
        self._lock = threading.Lock()
    
    # =========================================================================
    # MÓDULO 1: DETECCIÓN DE RED LOCAL
    # =========================================================================
    
    def detect_local_network(self) -> Optional[str]:
        """
        Detecta automáticamente el rango local de la red.
        
        Basado en la IP local, asume una máscara /24 (común en redes Docker).
        
        Returns:
            Rango CIDR (ej: "172.18.0.0/24") o None si falla
        """
        try:
            # Obtener IP local conectándose a 8.8.8.8 (sin enviar datos)
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            local_ip = s.getsockname()[0]
            s.close()
            
            # Generar rango CIDR /24 basado en los primeros 3 octetos
            octets = local_ip.split('.')
            network_cidr = f"{'.'.join(octets[:3])}.0/24"
            
            logger.info(f"[IPDiscovery] Red local detectada: {network_cidr} (IP local: {local_ip})")
            return network_cidr
            
        except Exception as e:
            logger.warning(f"[IPDiscovery] No se pudo detectar red local: {e}")
            return None
    
    # =========================================================================
    # MÓDULO 2: ESCANEO DE PUERTOS RÁPIDO
    # =========================================================================
    
    def scan_quick_ports(
        self,
        network_cidr: str,
        ports: Optional[List[int]] = None,
        max_hosts: int = 50
    ) -> Dict[int, List[str]]:
        """
        Escaneo RÁPIDO de puertos conocidos sin identificar servicios.
        Útil cuando necesitas velocidad máxima.
        
        Args:
            network_cidr: Rango en formato CIDR (ej: "172.18.0.0/24")
            ports: Lista de puertos a escanear (default: [5353, 8000])
            max_hosts: Máximo número de hosts a escanear
            
        Returns:
            Dict {puerto: [ips_con_puerto_abierto]}
        """
        if ports is None:
            ports = [5353, 8000]  # DNS y Storage/Processor
        
        try:
            network = IPv4Network(network_cidr, strict=False)
            hosts = list(network.hosts())[:max_hosts]
            
            logger.info(f"[IPDiscovery] Escaneo rápido: {len(hosts)} hosts, {len(ports)} puertos")
            
            results = {port: [] for port in ports}
            start_time = time.time()
            
            with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
                futures = []
                for ip in hosts:
                    for port in ports:
                        future = executor.submit(self._check_port_open, str(ip), port)
                        futures.append((future, port, str(ip)))
                
                for future, port, ip in futures:
                    try:
                        if future.result():
                            results[port].append(ip)
                    except Exception:
                        pass
            
            elapsed = time.time() - start_time
            total_found = sum(len(ips) for ips in results.values())
            logger.info(f"[IPDiscovery] Escaneo completado en {elapsed:.2f}s. Encontrados: {total_found} hosts con puertos abiertos")
            
            return results
            
        except ValueError as e:
            logger.error(f"[IPDiscovery] CIDR inválido '{network_cidr}': {e}")
            return {}
        except Exception as e:
            logger.error(f"[IPDiscovery] Error en escaneo rápido: {e}")
            return {}
    
    def _check_port_open(self, ip: str, port: int) -> bool:
        """
        Verifica si un puerto está abierto en un host (conexión TCP).
        
        Args:
            ip: Dirección IP del host
            port: Puerto a verificar
            
        Returns:
            True si el puerto está abierto, False en caso contrario
        """
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(self.timeout)
            result = sock.connect_ex((ip, port))
            sock.close()
            return result == 0
        except Exception:
            return False
    
    # =========================================================================
    # MÓDULO 3: IDENTIFICACIÓN DE SERVICIOS
    # =========================================================================
    
    def identify_service_batch(
        self,
        scan_results: Dict[int, List[str]]
    ) -> List[Dict]:
        """
        Identifica qué tipo de servicio corre en cada host encontrado.
        Realiza health checks HTTP para confirmar el tipo de servicio.
        
        Args:
            scan_results: Resultado de scan_quick_ports()
                         {puerto: [ips]}
            
        Returns:
            Lista de {ip, port, type, server_id, role, ...}
        """
        discovered = []
        
        if not requests:
            logger.warning("[IPDiscovery] requests no disponible, no se pueden identificar servicios")
            # Retornar hosts sin identificar
            for port, ips in scan_results.items():
                for ip in ips:
                    discovered.append({
                        "ip": ip,
                        "port": port,
                        "type": "unknown",
                        "responsive": True
                    })
            return discovered
        
        logger.info("[IPDiscovery] Identificando tipos de servicio...")
        
        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = []
            for port, ips in scan_results.items():
                for ip in ips:
                    future = executor.submit(self._identify_service, ip, port)
                    futures.append((future, ip, port))
            
            for future, ip, port in futures:
                try:
                    result = future.result()
                    if result:
                        discovered.append(result)
                    else:
                        # Puerto abierto pero no identificado
                        discovered.append({
                            "ip": ip,
                            "port": port,
                            "type": "unknown",
                            "responsive": True
                        })
                except Exception as e:
                    logger.debug(f"[IPDiscovery] Error identificando {ip}:{port}: {e}")
        
        logger.info(f"[IPDiscovery] Identificación completada: {len(discovered)} hosts analizados")
        return discovered
    
    def _identify_service(self, ip: str, port: int) -> Optional[Dict]:
        """
        Identifica qué tipo de servicio está corriendo en un host.
        Intenta diferentes endpoints HTTP.
        
        Args:
            ip: Dirección IP del host
            port: Puerto en el que escucha
            
        Returns:
            Dict con información del servicio o None
        """
        # Endpoints a probar y su tipo de servicio asociado
        endpoints = [
            ("/health", "dns"),
            ("/node/status", "storage"),
            ("/processor/status", "processor"),
        ]
        
        for endpoint, service_type in endpoints:
            try:
                url = f"http://{ip}:{port}{endpoint}"
                response = requests.get(url, timeout=0.3)
                
                if response.status_code == 200:
                    try:
                        data = response.json()
                        
                        # Procesar respuesta según tipo de servicio
                        if service_type == "dns":
                            return {
                                "ip": ip,
                                "port": port,
                                "type": "dns",
                                "server_id": data.get("server_id"),
                                "role": data.get("role", "unknown"),
                                "status": data.get("status", "ok"),
                                "health_endpoint": endpoint
                            }
                        
                        elif service_type == "storage":
                            node_info = data.get("node", {})
                            return {
                                "ip": ip,
                                "port": port,
                                "type": "storage",
                                "server_id": node_info.get("server_id"),
                                "role": node_info.get("role", "unknown"),
                                "status": "ok",
                                "health_endpoint": endpoint
                            }
                        
                        elif service_type == "processor":
                            return {
                                "ip": ip,
                                "port": port,
                                "type": "processor",
                                "server_id": data.get("processor_id"),
                                "status": data.get("status", "ok"),
                                "health_endpoint": endpoint
                            }
                    except (ValueError, KeyError):
                        # JSON inválido, pasar al siguiente endpoint
                        continue
                        
            except (requests.Timeout, requests.RequestException):
                continue
            except Exception:
                continue
        
        return None
    
    # =========================================================================
    # MÓDULO 4: PING SWEEP (Para identificar hosts activos)
    # =========================================================================
    
    def discover_with_ping(self, network_cidr: str, max_hosts: int = 50) -> List[str]:
        """
        Realiza ping sweep para identificar hosts activos.
        Luego escanea solo esos hosts (más eficiente).
        
        Args:
            network_cidr: Rango en formato CIDR
            max_hosts: Máximo número de hosts a pingear
            
        Returns:
            Lista de IPs activas (responden a ping)
        """
        try:
            network = IPv4Network(network_cidr, strict=False)
            hosts = list(network.hosts())[:max_hosts]
            active_hosts = []
            
            logger.info(f"[IPDiscovery] Ping sweep en {network_cidr} ({len(hosts)} hosts)...")
            start_time = time.time()
            
            with ThreadPoolExecutor(max_workers=30) as executor:
                futures = {
                    executor.submit(self._ping_host, str(ip)): str(ip)
                    for ip in hosts
                }
                
                for future in as_completed(futures, timeout=10):
                    try:
                        if future.result():
                            active_hosts.append(futures[future])
                    except Exception:
                        pass
            
            elapsed = time.time() - start_time
            logger.info(f"[IPDiscovery] Ping sweep completado en {elapsed:.2f}s. Hosts activos: {len(active_hosts)}")
            return active_hosts
            
        except Exception as e:
            logger.error(f"[IPDiscovery] Error en ping sweep: {e}")
            return []
    
    def _ping_host(self, ip: str) -> bool:
        """
        Envía ping ICMP a un host.
        
        Args:
            ip: Dirección IP del host
            
        Returns:
            True si responde al ping, False en caso contrario
        """
        try:
            result = subprocess.run(
                ["ping", "-c", "1", "-W", "1", ip],
                capture_output=True,
                timeout=2
            )
            return result.returncode == 0
        except Exception:
            return False


class DiscoveryManager:
    """
    Manager que orquesta las estrategias de descubrimiento.
    
    Flujo de descubrimiento:
    1. Detectar red local automáticamente
    2. Escaneo rápido de puertos conocidos
    3. Identificar tipos de servicio
    4. Opcional: Ping sweep si es necesario
    
    Todo sin requerir configuración especial de Docker.
    """
    
    def __init__(self, dns_port: int = 5353):
        self.dns_port = dns_port
        self.discovery = IPRangeDiscovery()
        self.discovered_cache: Dict[str, Dict] = {}
        self.last_discovery_time: float = 0
        self.cache_ttl: float = 300  # 5 minutos
        self._lock = threading.Lock()
    
    def discover_dns_servers(
        self,
        network_hint: Optional[str] = None,
        force_refresh: bool = False,
        use_ping_sweep: bool = False
    ) -> List[Dict]:
        """
        Descubre servidores DNS usando descubrimiento automático.
        
        Flujo:
        1. Usar cache si es fresco
        2. Detectar red local automáticamente (o usar network_hint)
        3. Escaneo rápido de puertos
        4. Identificar servicios
        5. Opcional: Ping sweep si es necesario
        
        Args:
            network_hint: Sugerencia de rango CIDR (opcional)
            force_refresh: Ignorar cache y hacer descubrimiento fresco
            use_ping_sweep: Usar ping sweep para pre-identificar hosts (más lento)
            
        Returns:
            Lista de servidores DNS descubiertos
        """
        # Verificar cache
        if not force_refresh and self._is_cache_valid():
            logger.info(f"[DiscoveryManager] Usando cache: {len(self.discovered_cache)} servidores DNS")
            return self._get_dns_servers_from_cache()
        
        discovered = []
        
        # Detectar red local
        network = network_hint or self.discovery.detect_local_network()
        if not network:
            logger.warning("[DiscoveryManager] No se pudo detectar red local")
            return []
        
        logger.info(f"[DiscoveryManager] Iniciando descubrimiento en {network}")
        start_time = time.time()
        
        try:
            # Estrategia 1: Escaneo rápido de puertos conocidos
            logger.debug("[DiscoveryManager] Fase 1: Escaneo de puertos conocidos")
            scan_results = self.discovery.scan_quick_ports(
                network,
                ports=[self.dns_port, 8000],
                max_hosts=50
            )
            
            # Estrategia 2: Identificar servicios
            logger.debug("[DiscoveryManager] Fase 2: Identificación de servicios")
            all_hosts = self.discovery.identify_service_batch(scan_results)
            
            # Filtrar solo los DNS
            dns_servers = [h for h in all_hosts if h.get("type") == "dns"]
            
            # Si no encontramos ningún DNS, intenta con ping sweep
            if not dns_servers and use_ping_sweep:
                logger.info("[DiscoveryManager] No se encontraron DNS. Intentando ping sweep...")
                active_hosts = self.discovery.discover_with_ping(network)
                if active_hosts:
                    # Escanear puertos en hosts activos
                    scan_results = {
                        5353: [ip for ip in active_hosts],
                        8000: [ip for ip in active_hosts]
                    }
                    all_hosts = self.discovery.identify_service_batch(scan_results)
                    dns_servers = [h for h in all_hosts if h.get("type") == "dns"]
            
            elapsed = time.time() - start_time
            
            if dns_servers:
                # Guardar en cache
                with self._lock:
                    self.discovered_cache.clear()
                    for srv in dns_servers:
                        key = srv.get("ip")
                        if key:
                            self.discovered_cache[key] = srv
                    self.last_discovery_time = time.time()
                
                logger.info(f"[DiscoveryManager] Descubrimiento completado en {elapsed:.2f}s: {len(dns_servers)} DNS encontrados")
                return dns_servers
            else:
                logger.warning(f"[DiscoveryManager] No se encontraron servidores DNS después de {elapsed:.2f}s")
                return []
        
        except Exception as e:
            logger.error(f"[DiscoveryManager] Error en descubrimiento: {e}")
            return []
    
    def discover_all_services(
        self,
        network_hint: Optional[str] = None,
        force_refresh: bool = False
    ) -> Dict[str, List[Dict]]:
        """
        Descubre TODOS los servicios (DNS, Storage, Processor).
        
        Returns:
            Dict {tipo_servicio: [lista_de_servidores]}
        """
        network = network_hint or self.discovery.detect_local_network()
        if not network:
            logger.warning("[DiscoveryManager] No se pudo detectar red local")
            return {"dns": [], "storage": [], "processor": []}
        
        logger.info(f"[DiscoveryManager] Descubriendo todos los servicios en {network}")
        
        # Escaneo rápido
        scan_results = self.discovery.scan_quick_ports(network)
        all_hosts = self.discovery.identify_service_batch(scan_results)
        
        # Agrupar por tipo
        result = {
            "dns": [h for h in all_hosts if h.get("type") == "dns"],
            "storage": [h for h in all_hosts if h.get("type") == "storage"],
            "processor": [h for h in all_hosts if h.get("type") == "processor"],
            "unknown": [h for h in all_hosts if h.get("type") == "unknown"]
        }
        
        logger.info(f"[DiscoveryManager] Descubrimiento: {len(result['dns'])} DNS, {len(result['storage'])} Storage, {len(result['processor'])} Processor")
        
        return result
    
    def _is_cache_valid(self) -> bool:
        """Verifica si el cache sigue siendo válido"""
        return (
            self.discovered_cache and 
            time.time() - self.last_discovery_time < self.cache_ttl
        )
    
    def _get_dns_servers_from_cache(self) -> List[Dict]:
        """Retorna lista de DNS desde el cache"""
        with self._lock:
            return list(self.discovered_cache.values())
    
    def clear_cache(self) -> None:
        """Limpia el cache de descubrimiento"""
        with self._lock:
            self.discovered_cache.clear()
            self.last_discovery_time = 0
        logger.info("[DiscoveryManager] Cache de descubrimiento limpiado")
