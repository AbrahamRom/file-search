import socket
import logging
import os
import time
import asyncio
from typing import Dict, Optional, List, Set
from datetime import datetime
from fastapi import FastAPI, HTTPException, BackgroundTasks, Request
from pydantic import BaseModel
import httpx
from .logging_config import configure_logging

# Configurar logs al iniciar
configure_logging()
logger = logging.getLogger("dns_service")

app = FastAPI(title="Internal DNS Service HA", version="3.0.0")

# ============================================================================
# CONFIGURACIÓN DEL SERVIDOR
# ============================================================================

# Identificador único del servidor (DEBE ser único por instancia)
server_id = os.getenv("DNS_SERVER_ID", f"dns_{socket.gethostname()}")

# Alias común para descubrimiento via Docker DNS (todos los DNS comparten este alias)
DNS_ALIAS = os.getenv("DNS_ALIAS", "dns")

# Puerto DNS
dns_port = int(os.getenv("DNS_PORT", 5353))

# Intervalos de configuración
HEALTH_CHECK_INTERVAL = int(os.getenv("HEALTH_CHECK_INTERVAL", 10))  # 10 segundos
SYNC_INTERVAL = int(os.getenv("SYNC_INTERVAL", 30))  # 30 segundos
DISCOVERY_INTERVAL = int(os.getenv("DISCOVERY_INTERVAL", 15))  # Re-descubrir cada 15s

# Timeout para considerar un servidor DNS como caído y eliminarlo del registro (en segundos)
DNS_SERVER_TIMEOUT = int(os.getenv("DNS_SERVER_TIMEOUT", 30))  # 30 segundos sin respuesta = eliminar

# ============================================================================
# ESTADO DEL SERVIDOR
# ============================================================================

# Rol actual del servidor (se determina dinámicamente)
server_role = "unknown"  # primary, backup, unknown

# Timestamp de cuando me convertí en primario (para resolver conflictos)
primary_since: Optional[float] = None

# Cache de resoluciones DNS
dns_cache: Dict[str, dict] = {}

# Estado del clúster DNS (descubierto dinámicamente)
# {server_id: {url, hostname, healthy, role, last_seen, primary_since}}
cluster_state: Dict[str, dict] = {}

# IPs conocidas de servidores DNS (para evitar duplicados)
known_dns_ips: Set[str] = set()

# Estado de sincronización
sync_status = {
    "last_sync": None,
    "sync_count": 0,
    "is_syncing": False
}

# URL del primario actual
current_primary_url: Optional[str] = None

# Mi propia IP y hostname
my_ip: Optional[str] = None
my_hostname: Optional[str] = None

start_time = time.time()

# ============================================================================
# GESTIÓN DE SERVIDORES API (file-search servers)
# ============================================================================

# Estado de los servidores API registrados
# {server_id: {ip, port, role, last_heartbeat, registered_at, primary_epoch, lease_expires_at}}
api_servers: Dict[str, dict] = {}

# Epoch global para PRIMARY (monotónico)
primary_epoch: int = 0

# Duración del lease en segundos
LEASE_DURATION = int(os.getenv("LEASE_DURATION", 30))

# Estado de los PROCESSOR nodes registrados
# {processor_id: {ip, port, last_heartbeat, registered_at, healthy}}
processor_servers: Dict[str, dict] = {}

# Timeout para considerar un servidor API como caído (en segundos)
API_SERVER_TIMEOUT = int(os.getenv("API_SERVER_TIMEOUT", 25))

# Timeout para processors (en segundos)
PROCESSOR_TIMEOUT = int(os.getenv("PROCESSOR_TIMEOUT", 25))

# Lock para operaciones atómicas en api_servers
api_servers_lock = asyncio.Lock()

# Lock para operaciones atómicas en processor_servers
processor_servers_lock = asyncio.Lock()

# Timestamp de la última verificación de split-brain
last_split_brain_check: float = 0
SPLIT_BRAIN_CHECK_INTERVAL = 5  # Verificar cada 5 segundos

# Variables para el algoritmo de bully
election_in_progress: bool = False
election_timeout: float = 5.0  # Timeout para considerar respuestas en elección
last_election_time: float = 0
ELECTION_COOLDOWN = 10  # Cooldown entre elecciones (en segundos)


# ============================================================================
# MODELOS PYDANTIC
# ============================================================================

class ResolutionResponse(BaseModel):
    hostname: str
    ip: str
    ttl: int
    server_id: str
    role: str


class SyncData(BaseModel):
    cache: Dict[str, dict]
    timestamp: str
    source_id: str
    api_servers: Optional[Dict[str, dict]] = None
    processor_servers: Optional[Dict[str, dict]] = None


class HealthResponse(BaseModel):
    status: str
    service: str
    role: str
    server_id: str
    ip: Optional[str]
    cache_size: int
    last_sync: Optional[str]
    uptime: float
    primary_since: Optional[float]


class DNSServerInfo(BaseModel):
    server_id: str
    url: str
    ip: str
    role: str
    healthy: bool
    primary_since: Optional[float]


class DNSServersResponse(BaseModel):
    primary: Optional[DNSServerInfo]
    backups: List[DNSServerInfo]
    all_servers: List[DNSServerInfo]
    dns_alias: str
    timestamp: str


class ClusterStatusResponse(BaseModel):
    server_id: str
    role: str
    my_ip: Optional[str]
    cluster_members: Dict[str, dict]
    primary_url: Optional[str]
    cache_size: int
    last_sync: Optional[str]
    uptime: float
    dns_alias: str


class RegisterRequest(BaseModel):
    server_id: str
    ip: str
    hostname: str
    role: str
    primary_since: Optional[float] = None


# ============================================================================
# MODELOS PARA SERVIDORES API
# ============================================================================

class APIServerRegisterRequest(BaseModel):
    server_id: str
    ip: str
    port: int = 8000


class APIServerHeartbeatRequest(BaseModel):
    server_id: str
    current_role: str


class APIServerInfo(BaseModel):
    server_id: str
    ip: str
    port: int
    role: str
    last_heartbeat: str
    registered_at: str
    url: str


class APIServerResolveResponse(BaseModel):
    server_id: str
    ip: str
    port: int
    role: str
    url: str
    primary_epoch: Optional[int] = None
    lease_expires_at: Optional[str] = None


# ============================================================================
# MODELOS PARA PROCESSOR NODES
# ============================================================================

class ProcessorRegisterRequest(BaseModel):
    processor_id: str
    ip: str
    port: int = 8000
    external_port: Optional[int] = None  # Puerto accesible desde fuera de Docker
    external_ip: Optional[str] = None  # IP accesible desde fuera de Docker (auto-detectada si no se proporciona)


class ProcessorHeartbeatRequest(BaseModel):
    processor_id: str


class ProcessorResolveResponse(BaseModel):
    processor_id: str
    ip: str
    port: int
    url: str
    external_ip: Optional[str] = None
    external_port: Optional[int] = None


# ============================================================================
# ENDPOINTS
# ============================================================================

@app.get("/health", response_model=HealthResponse)
def health():
    """Endpoint de salud del servicio DNS"""
    return HealthResponse(
        status="ok",
        service="dns-resolver-ha",
        role=server_role,
        server_id=server_id,
        ip=my_ip,
        cache_size=len(dns_cache),
        last_sync=sync_status["last_sync"],
        uptime=time.time() - start_time,
        primary_since=primary_since
    )


@app.get("/dns-servers", response_model=DNSServersResponse)
def get_dns_servers():
    """
    Retorna la lista de todos los servidores DNS disponibles.
    Los clientes usan este endpoint para obtener la lista de failover.
    """
    logger.info(f"[{server_id}] Solicitud de lista de servidores DNS")
    
    primary = None
    backups = []
    all_servers = []
    
    for sid, info in cluster_state.items():
        server_info = DNSServerInfo(
            server_id=sid,
            url=info.get("url", ""),
            ip=info.get("ip", ""),
            role=info.get("role", "unknown"),
            healthy=info.get("healthy", False),
            primary_since=info.get("primary_since")
        )
        all_servers.append(server_info)
        
        if info.get("role") == "primary" and info.get("healthy"):
            primary = server_info
        elif info.get("healthy"):
            backups.append(server_info)
    
    # Ordenar backups por primary_since (más antiguo primero como fallback)
    backups.sort(key=lambda x: x.primary_since or float('inf'))
    
    return DNSServersResponse(
        primary=primary,
        backups=backups,
        all_servers=all_servers,
        dns_alias=DNS_ALIAS,
        timestamp=datetime.now().isoformat()
    )


@app.get("/cluster-status", response_model=ClusterStatusResponse)
def get_cluster_status():
    """Retorna el estado completo del clúster DNS"""
    return ClusterStatusResponse(
        server_id=server_id,
        role=server_role,
        my_ip=my_ip,
        cluster_members=cluster_state,
        primary_url=current_primary_url,
        cache_size=len(dns_cache),
        last_sync=sync_status["last_sync"],
        uptime=time.time() - start_time,
        dns_alias=DNS_ALIAS
    )


@app.get("/resolve/{hostname}", response_model=ResolutionResponse)
async def resolve_hostname(hostname: str, background_tasks: BackgroundTasks):
    """Resuelve un nombre de host a su dirección IP"""
    logger.info(f"[{server_id}][{server_role}] Solicitud de resolución: {hostname}")
    
    # Verificar cache primero
    if hostname in dns_cache:
        cache_entry = dns_cache[hostname]
        if time.time() - cache_entry["timestamp"] < cache_entry["ttl"]:
            logger.info(f"[{server_id}] Cache HIT: {hostname} -> {cache_entry['ip']}")
            return ResolutionResponse(
                hostname=hostname,
                ip=cache_entry["ip"],
                ttl=cache_entry["ttl"],
                server_id=server_id,
                role=server_role
            )
    
    try:
        ip_address = socket.gethostbyname(hostname)
        
        dns_cache[hostname] = {
            "ip": ip_address,
            "ttl": 300,
            "timestamp": time.time()
        }
        
        logger.info(f"[{server_id}] Resolución exitosa: {hostname} -> {ip_address}")
        
        if server_role == "primary":
            background_tasks.add_task(propagate_to_backups, hostname, ip_address)
        
        return ResolutionResponse(
            hostname=hostname,
            ip=ip_address,
            ttl=300,
            server_id=server_id,
            role=server_role
        )
        
    except socket.gaierror as e:
        logger.warning(f"[{server_id}] No se pudo resolver '{hostname}': {e}")
        raise HTTPException(status_code=404, detail=f"Hostname '{hostname}' not found")
    except Exception as e:
        logger.error(f"[{server_id}] Error inesperado: {e}")
        raise HTTPException(status_code=500, detail="Internal DNS error")


@app.post("/sync")
async def receive_sync(sync_data: SyncData, background_tasks: BackgroundTasks):
    """Recibe actualizaciones de sincronización desde otro DNS (primario u otro nodo)"""
    logger.info(f"[{server_id}] Recibiendo sync desde {sync_data.source_id}")
    
    # Sincronizar cache DNS
    for hostname, entry in sync_data.cache.items():
        dns_cache[hostname] = entry
    
    # Sincronizar api_servers (storage nodes)
    if sync_data.api_servers:
        async with api_servers_lock:
            for sid, info in sync_data.api_servers.items():
                # Solo actualizar si es más reciente o no existe
                if sid not in api_servers:
                    api_servers[sid] = info
                    logger.debug(f"[{server_id}] Sync: añadido api_server {sid}")
                else:
                    # Comparar timestamps para mantener el más reciente
                    existing_hb = api_servers[sid].get("last_heartbeat", "")
                    incoming_hb = info.get("last_heartbeat", "")
                    if incoming_hb > existing_hb:
                        api_servers[sid] = info
    
    # Sincronizar processor_servers
    if sync_data.processor_servers:
        async with processor_servers_lock:
            for pid, info in sync_data.processor_servers.items():
                if pid not in processor_servers:
                    processor_servers[pid] = info
                    logger.debug(f"[{server_id}] Sync: añadido processor {pid}")
                else:
                    existing_hb = processor_servers[pid].get("last_heartbeat", "")
                    incoming_hb = info.get("last_heartbeat", "")
                    if incoming_hb > existing_hb:
                        processor_servers[pid] = info
    
    sync_status["last_sync"] = datetime.now().isoformat()
    sync_status["sync_count"] += 1
    
    logger.info(f"[{server_id}] Sync completada. Cache: {len(dns_cache)}, API servers: {len(api_servers)}, Processors: {len(processor_servers)}")
    
    # VERIFICACIÓN SPLIT-BRAIN: Después de recibir sync, verificar consistencia
    background_tasks.add_task(resolve_split_brain)
    
    return {
        "status": "synced", 
        "entries": len(dns_cache),
        "api_servers": len(api_servers),
        "processors": len(processor_servers),
        "receiver": server_id
    }


@app.get("/cache")
async def get_cache():
    """Obtiene el estado completo del DNS (cache, api_servers, processor_servers)"""
    async with api_servers_lock:
        api_copy = dict(api_servers)
    async with processor_servers_lock:
        proc_copy = dict(processor_servers)
    
    return {
        "cache": dns_cache,
        "api_servers": api_copy,
        "processor_servers": proc_copy,
        "timestamp": datetime.now().isoformat(),
        "server_id": server_id,
        "role": server_role
    }


@app.post("/register")
async def register_dns_server(request: RegisterRequest):
    """
    Endpoint para que otros servidores DNS se registren.
    Permite descubrimiento mutuo.
    """
    logger.info(f"[{server_id}] Registro de servidor DNS: {request.server_id} ({request.ip})")
    
    cluster_state[request.server_id] = {
        "url": f"http://{request.ip}:{dns_port}",
        "ip": request.ip,
        "hostname": request.hostname,
        "healthy": True,
        "role": request.role,
        "primary_since": request.primary_since,
        "last_seen": datetime.now().isoformat()
    }
    known_dns_ips.add(request.ip)
    
    # Retornar mi información y el estado del clúster
    return {
        "status": "registered",
        "my_id": server_id,
        "my_ip": my_ip,
        "my_role": server_role,
        "primary_since": primary_since,
        "cluster_size": len(cluster_state)
    }


# ============================================================================
# ENDPOINTS PARA SERVIDORES API (file-search servers)
# ============================================================================

@app.post("/server/register")
async def register_api_server(request: APIServerRegisterRequest, background_tasks: BackgroundTasks):
    """
    Registra un servidor API y le asigna un rol (PRIMARY o BACKUP).
    
    Comportamiento:
    - Primer servidor en registrarse -> PRIMARY
    - Servidores subsecuentes -> BACKUP
    - Si un nodo caído vuelve (re-registración) -> BACKUP (si ya hay PRIMARY activo)
    
    El registro se propaga automáticamente a todos los otros DNS.
    """
    async with api_servers_lock:
        now = datetime.now().isoformat()
        now_dt = datetime.now()
        
        # Verificar si este servidor ya estaba registrado (re-registración)
        is_reregistration = request.server_id in api_servers
        old_role = api_servers.get(request.server_id, {}).get("role", None) if is_reregistration else None
        
        if is_reregistration:
            logger.info(
                f"[{server_id}] Storage node {request.server_id} re-registrándose "
                f"(rol anterior: {old_role})"
            )
        
        # Verificar si ya hay un PRIMARY activo y vivo
        current_primary = None
        for sid, info in api_servers.items():
            if info["role"] == "PRIMARY":
                # Verificar si el PRIMARY está vivo
                last_hb = datetime.fromisoformat(info["last_heartbeat"])
                elapsed = (now_dt - last_hb).total_seconds()
                if elapsed < API_SERVER_TIMEOUT:
                    current_primary = sid
                    break
        
        # Asignar rol y epoch/lease
        global primary_epoch
        
        # Si era PRIMARY y se está re-registrando, pero ya hay otro PRIMARY activo,
        # debe volver como BACKUP
        if is_reregistration and old_role == "PRIMARY" and current_primary and current_primary != request.server_id:
            assigned_role = "BACKUP"
            lease_expires = None
            logger.warning(
                f"[{server_id}] Storage node {request.server_id} era PRIMARY pero ahora hay "
                f"un nuevo PRIMARY ({current_primary}). Re-registrando como BACKUP."
            )
        elif current_primary is None:
            # No hay PRIMARY activo, este será el PRIMARY
            assigned_role = "PRIMARY"
            primary_epoch += 1
            lease_expires = now_dt.timestamp() + LEASE_DURATION
            logger.info(
                f"[{server_id}] Storage node {request.server_id} registrado como PRIMARY "
                f"(epoch={primary_epoch}, re-registración={is_reregistration})"
            )
        else:
            # Ya hay un PRIMARY, este será BACKUP
            assigned_role = "BACKUP"
            lease_expires = None
            logger.info(
                f"[{server_id}] Storage node {request.server_id} registrado como BACKUP "
                f"(PRIMARY actual: {current_primary}, re-registración={is_reregistration})"
            )
        
        # Preservar registered_at original si es re-registración (para mantener antigüedad)
        original_registered_at = api_servers.get(request.server_id, {}).get("registered_at", now) if is_reregistration else now
        
        # Registrar o actualizar servidor
        server_info = {
            "ip": request.ip,
            "port": request.port,
            "role": assigned_role,
            "last_heartbeat": now,
            "registered_at": original_registered_at,  # Preservar timestamp original
            "primary_epoch": primary_epoch if assigned_role == "PRIMARY" else None,
            "lease_expires_at": datetime.fromtimestamp(lease_expires).isoformat() if lease_expires else None
        }
        api_servers[request.server_id] = server_info
        
        # Propagar a otros DNS en background
        background_tasks.add_task(
            propagate_service_registration, 
            "api_server", 
            request.server_id, 
            server_info
        )
        
        # Obtener info del PRIMARY actual para que los backups sepan a quién sincronizar
        primary_info = None
        if assigned_role == "BACKUP" and current_primary:
            p = api_servers[current_primary]
            primary_info = {
                "server_id": current_primary,
                "ip": p["ip"],
                "port": p["port"],
                "url": f"http://{p['ip']}:{p['port']}"
            }
        
        # Incluir epoch y lease en la respuesta para PRIMARY
        response_epoch = server_info.get("primary_epoch") if assigned_role == "PRIMARY" else None
        response_lease = server_info.get("lease_expires_at") if assigned_role == "PRIMARY" else None
    
    # VERIFICACIÓN SPLIT-BRAIN: Ejecutar fuera del lock
    background_tasks.add_task(resolve_split_brain)
    
    return {
        "status": "registered",
        "assigned_role": assigned_role,
        "server_id": request.server_id,
        "primary_info": primary_info,
        "primary_epoch": response_epoch,
        "lease_expires_at": response_lease,
        "total_servers": len(api_servers)
    }


@app.post("/server/heartbeat")
async def api_server_heartbeat(request: APIServerHeartbeatRequest, background_tasks: BackgroundTasks):
    """
    Recibe heartbeat de un servidor API y verifica/actualiza su rol.
    Maneja la promoción de BACKUPs a PRIMARY si es necesario.
    
    IMPORTANTE: Esta función detecta cuando el PRIMARY actual ha caído y
    promueve automáticamente un BACKUP. No asume que storage_1 es el PRIMARY.
    
    Si un nodo fue eliminado (por timeout) y vuelve, debe re-registrarse primero.
    """
    async with api_servers_lock:
        if request.server_id not in api_servers:
            # El servidor no está registrado (fue eliminado o nunca se registró)
            logger.warning(
                f"[{server_id}] Storage node {request.server_id} envió heartbeat pero no está "
                f"registrado. Debe re-registrarse usando /server/register"
            )
            raise HTTPException(
                status_code=404, 
                detail=f"Servidor {request.server_id} no registrado. Debe registrarse primero usando /server/register"
            )
        
        now = datetime.now()
        now_iso = now.isoformat()
        
        # Verificar estado del PRIMARY actual (si existe alguno)
        current_primary = None
        primary_alive = False
        
        for sid, info in api_servers.items():
            if info["role"] == "PRIMARY":
                current_primary = sid
                last_hb = datetime.fromisoformat(info["last_heartbeat"])
                elapsed = (now - last_hb).total_seconds()
                primary_alive = elapsed < API_SERVER_TIMEOUT
                
                # Log detallado del estado del PRIMARY
                if not primary_alive:
                    logger.warning(
                        f"[{server_id}] PRIMARY actual {sid} está caído o sin respuesta "
                        f"(último heartbeat hace {elapsed:.1f}s > timeout {API_SERVER_TIMEOUT}s)"
                    )
                break
        
        # Actualizar heartbeat del servidor que envía
        api_servers[request.server_id]["last_heartbeat"] = now_iso
        
        assigned_role = api_servers[request.server_id]["role"]
        
        # Renovar lease si es PRIMARY
        global primary_epoch
        if assigned_role == "PRIMARY":
            lease_expires = now.timestamp() + LEASE_DURATION
            api_servers[request.server_id]["lease_expires_at"] = datetime.fromtimestamp(lease_expires).isoformat()
        
        primary_info = None
        
        affected_service_ids: set[str] = {request.server_id}

        # LÓGICA DE FAILOVER: Si no hay PRIMARY vivo, promover un BACKUP
        if not primary_alive:
            # Degradar el PRIMARY caído (si existe y no es el que está reportando)
            if current_primary and current_primary != request.server_id:
                logger.warning(
                    f"[{server_id}] Degradando PRIMARY caído {current_primary} a BACKUP "
                    f"(no responde > {API_SERVER_TIMEOUT}s)"
                )
                api_servers[current_primary]["role"] = "BACKUP"
                api_servers[current_primary]["primary_epoch"] = None
                api_servers[current_primary]["lease_expires_at"] = None
                affected_service_ids.add(current_primary)
            
            # Elegir el BACKUP más antiguo (por registered_at) que esté vivo para promover
            oldest_backup = None
            oldest_time = None
            
            for sid, info in api_servers.items():
                # Verificar que el nodo esté vivo (heartbeat reciente)
                if sid == request.server_id:
                    # El que está reportando siempre se considera vivo
                    is_alive = True
                else:
                    last_hb = datetime.fromisoformat(info["last_heartbeat"])
                    elapsed = (now - last_hb).total_seconds()
                    is_alive = elapsed < API_SERVER_TIMEOUT
                
                if not is_alive:
                    continue
                
                reg_time = datetime.fromisoformat(info.get("registered_at", now_iso))
                if oldest_time is None or reg_time < oldest_time:
                    oldest_backup = sid
                    oldest_time = reg_time
            
            if oldest_backup:
                # Promover el backup más antiguo con nuevo epoch
                primary_epoch += 1
                lease_expires = now.timestamp() + LEASE_DURATION
                api_servers[oldest_backup]["role"] = "PRIMARY"
                api_servers[oldest_backup]["primary_epoch"] = primary_epoch
                api_servers[oldest_backup]["lease_expires_at"] = datetime.fromtimestamp(lease_expires).isoformat()
                
                logger.warning(
                    f"[{server_id}] *** FAILOVER EN HEARTBEAT: Promoviendo {oldest_backup} a PRIMARY "
                    f"(epoch={primary_epoch}, registrado: {oldest_time}) ***"
                )
                
                affected_service_ids.add(oldest_backup)
                
                if request.server_id == oldest_backup:
                    assigned_role = "PRIMARY"
            elif request.server_id == current_primary:
                # El PRIMARY anterior vuelve a estar activo (era el que reportaba)
                assigned_role = "PRIMARY"
                api_servers[request.server_id]["role"] = "PRIMARY"
                logger.info(f"[{server_id}] PRIMARY {request.server_id} vuelve a estar activo")
        
        # Si soy BACKUP, obtener info del PRIMARY
        if assigned_role == "BACKUP":
            for sid, info in api_servers.items():
                if info["role"] == "PRIMARY":
                    primary_info = {
                        "server_id": sid,
                        "ip": info["ip"],
                        "port": info["port"],
                        "url": f"http://{info['ip']}:{info['port']}"
                    }
                    break

        # Propagar heartbeat/rol actualizado a otros DNS en background
        for sid in affected_service_ids:
            if sid in api_servers:
                server_info = dict(api_servers[sid])
                background_tasks.add_task(
                    propagate_service_registration,
                    "api_server",
                    sid,
                    server_info,
                )
        
        # Obtener epoch y lease del servidor actual para incluirlo en la respuesta
        current_server_info = api_servers.get(request.server_id, {})
        response_epoch = current_server_info.get("primary_epoch") if assigned_role == "PRIMARY" else None
        response_lease = current_server_info.get("lease_expires_at") if assigned_role == "PRIMARY" else None
    
    # VERIFICACIÓN SPLIT-BRAIN: Ejecutar fuera del lock para evitar deadlock
    background_tasks.add_task(resolve_split_brain)
    
    return {
        "status": "ok",
        "assigned_role": assigned_role,
        "primary_info": primary_info,
        "primary_epoch": response_epoch,
        "lease_expires_at": response_lease,
        "timestamp": now_iso
    }


@app.get("/server/resolve", response_model=APIServerResolveResponse)
async def resolve_api_server():
    """
    Resuelve la IP del servidor API PRIMARY actual.
    Usado por los clientes para saber a qué servidor conectarse.
    
    IMPORTANTE: Esta función NO asume que storage_1 es el PRIMARY.
    Busca dinámicamente cuál nodo tiene el rol PRIMARY y está vivo.
    Si el PRIMARY está caído, promueve automáticamente un BACKUP.
    """
    async with api_servers_lock:
        now = datetime.now()
        
        # Primero verificar si hay algún PRIMARY registrado y si está vivo
        current_primary_sid = None
        current_primary_info = None
        primary_is_alive = False
        
        for sid, info in api_servers.items():
            if info["role"] == "PRIMARY":
                current_primary_sid = sid
                current_primary_info = info
                
                last_hb = datetime.fromisoformat(info["last_heartbeat"])
                elapsed = (now - last_hb).total_seconds()
                primary_is_alive = elapsed < API_SERVER_TIMEOUT
                
                if primary_is_alive:
                    # PRIMARY está vivo, devolverlo
                    logger.info(
                        f"[{server_id}] Resolución de servidor API -> PRIMARY {sid} "
                        f"({info['ip']}:{info['port']}) - último heartbeat hace {elapsed:.1f}s"
                    )
                    return APIServerResolveResponse(
                        server_id=sid,
                        ip=info["ip"],
                        port=info["port"],
                        role="PRIMARY",
                        url=f"http://{info['ip']}:{info['port']}",
                        primary_epoch=info.get("primary_epoch"),
                        lease_expires_at=info.get("lease_expires_at")
                    )
                else:
                    # PRIMARY está caído
                    logger.warning(
                        f"[{server_id}] PRIMARY {sid} está caído "
                        f"(último heartbeat hace {elapsed:.1f}s > timeout {API_SERVER_TIMEOUT}s)"
                    )
                    break
        
        # Si llegamos aquí, no hay PRIMARY vivo. Buscar un BACKUP para promover
        logger.warning(f"[{server_id}] No hay PRIMARY vivo. Buscando BACKUP para promover...")
        
        # Degradar el PRIMARY caído a BACKUP (si existe)
        if current_primary_sid and not primary_is_alive:
            logger.warning(
                f"[{server_id}] Degradando {current_primary_sid} de PRIMARY a BACKUP "
                f"(sin heartbeat por {(now - datetime.fromisoformat(current_primary_info['last_heartbeat'])).total_seconds():.1f}s)"
            )
            api_servers[current_primary_sid]["role"] = "BACKUP"
        
        # Buscar el BACKUP más antiguo (por registered_at) que esté vivo
        best_backup = None
        best_backup_sid = None
        oldest_registration = None
        
        for sid, info in api_servers.items():
            last_hb = datetime.fromisoformat(info["last_heartbeat"])
            elapsed = (now - last_hb).total_seconds()
            
            # Solo considerar nodos vivos
            if elapsed >= API_SERVER_TIMEOUT:
                continue
            
            reg_time = datetime.fromisoformat(info.get("registered_at", now.isoformat()))
            
            if oldest_registration is None or reg_time < oldest_registration:
                oldest_registration = reg_time
                best_backup = info
                best_backup_sid = sid
        
        # Si encontramos un BACKUP vivo, promoverlo
        if best_backup_sid:
            global primary_epoch
            primary_epoch += 1
            lease_expires = now.timestamp() + LEASE_DURATION
            
            api_servers[best_backup_sid]["role"] = "PRIMARY"
            api_servers[best_backup_sid]["primary_epoch"] = primary_epoch
            api_servers[best_backup_sid]["lease_expires_at"] = datetime.fromtimestamp(lease_expires).isoformat()
            
            logger.warning(
                f"[{server_id}] *** FAILOVER AUTOMÁTICO: Promoviendo {best_backup_sid} "
                f"({best_backup['ip']}:{best_backup['port']}) a PRIMARY (epoch={primary_epoch}) ***"
            )
            
            return APIServerResolveResponse(
                server_id=best_backup_sid,
                ip=best_backup["ip"],
                port=best_backup["port"],
                role="PRIMARY",
                url=f"http://{best_backup['ip']}:{best_backup['port']}",
                primary_epoch=primary_epoch,
                lease_expires_at=api_servers[best_backup_sid]["lease_expires_at"]
            )
        
        # No hay ningún storage node disponible
        logger.error(f"[{server_id}] No hay servidores API (storage nodes) disponibles en el clúster")
        logger.error(f"[{server_id}] Storage nodes registrados: {list(api_servers.keys())}")
        raise HTTPException(
            status_code=503, 
            detail="No hay servidores API disponibles. Todos los storage nodes están caídos."
        )


@app.get("/server/list")
async def list_api_servers():
    """Lista todos los servidores API registrados con su estado."""
    async with api_servers_lock:
        now = datetime.now()
        servers = []
        
        for sid, info in api_servers.items():
            last_hb = datetime.fromisoformat(info["last_heartbeat"])
            elapsed = (now - last_hb).total_seconds()
            
            servers.append({
                "server_id": sid,
                "ip": info["ip"],
                "port": info["port"],
                "role": info["role"],
                "last_heartbeat": info["last_heartbeat"],
                "registered_at": info["registered_at"],
                "url": f"http://{info['ip']}:{info['port']}",
                "alive": elapsed < API_SERVER_TIMEOUT,
                "seconds_since_heartbeat": int(elapsed)
            })
        
        return {
            "servers": servers,
            "total": len(servers),
            "timestamp": now.isoformat()
        }


# ============================================================================
# ENDPOINTS PARA PROCESSOR NODES
# ============================================================================

@app.post("/processor/register")
async def register_processor(request: ProcessorRegisterRequest, background_tasks: BackgroundTasks, http_request: Request):
    """
    Registra un Processor Node en el DNS.
    Los processors son stateless y todos tienen el mismo peso.
    El registro se propaga automáticamente a todos los otros DNS.
    
    La IP externa se detecta automáticamente desde la que se registra el processor.
    Si se proporciona external_ip explícitamente, se usa esa en lugar de auto-detectar.
    """
    async with processor_servers_lock:
        now = datetime.now().isoformat()
        
        # Detectar IP del cliente que se registra (IP desde la que se conecta el processor)
        # Esto permite que el DNS sepa la IP pública/accesible del host del processor
        client_ip = http_request.client.host if http_request.client else "localhost"
        
        # Usar IP explícita si se proporciona, sino usar auto-detectada del cliente
        detected_external_ip = request.external_ip or client_ip
        
        processor_info = {
            "ip": request.ip,
            "port": request.port,
            "external_port": request.external_port or request.port,
            "external_ip": detected_external_ip,
            "last_heartbeat": now,
            "registered_at": now,
            "healthy": True
        }
        
        processor_servers[request.processor_id] = processor_info
        
        logger.info(
            f"[{server_id}] Processor {request.processor_id} registrado "
            f"(interno:{request.ip}:{request.port}, externo:{detected_external_ip}:{processor_info['external_port']})"
        )
        
        # Propagar a otros DNS en background
        background_tasks.add_task(
            propagate_service_registration, 
            "processor", 
            request.processor_id, 
            processor_info
        )
        
        return {
            "status": "registered",
            "processor_id": request.processor_id,
            "external_ip": detected_external_ip,
            "external_port": processor_info["external_port"],
            "total_processors": len(processor_servers)
        }


@app.post("/processor/heartbeat")
async def processor_heartbeat(request: ProcessorHeartbeatRequest, background_tasks: BackgroundTasks):
    """
    Recibe heartbeat de un Processor para mantenerlo activo.
    También propaga el heartbeat a otros DNS.
    """
    async with processor_servers_lock:
        if request.processor_id not in processor_servers:
            raise HTTPException(
                status_code=404,
                detail=f"Processor {request.processor_id} no registrado. Debe registrarse primero."
            )
        
        now_iso = datetime.now().isoformat()
        processor_servers[request.processor_id]["last_heartbeat"] = now_iso
        processor_servers[request.processor_id]["healthy"] = True
        
        # Propagar heartbeat a otros DNS en background
        processor_info = dict(processor_servers[request.processor_id])
        background_tasks.add_task(
            propagate_service_registration, 
            "processor", 
            request.processor_id, 
            processor_info
        )
        
        return {
            "status": "ok",
            "processor_id": request.processor_id,
            "timestamp": now_iso
        }


@app.get("/processor/resolve", response_model=ProcessorResolveResponse)
async def resolve_processor():
    """
    Resuelve un Processor Node disponible.
    Usa round-robin simple entre los processors activos.
    Retorna también la IP externa y puerto externo para acceso desde fuera de Docker.
    """
    async with processor_servers_lock:
        now = datetime.now()
        active_processors = []
        
        # Filtrar processors activos
        for pid, info in processor_servers.items():
            last_hb = datetime.fromisoformat(info["last_heartbeat"])
            elapsed = (now - last_hb).total_seconds()
            
            if elapsed < PROCESSOR_TIMEOUT:
                active_processors.append((pid, info))
        
        if not active_processors:
            logger.error(f"[{server_id}] No hay processors disponibles")
            raise HTTPException(status_code=503, detail="No hay processors disponibles")
        
        # Selección aleatoria para balanceo de carga
        import random
        pid, info = random.choice(active_processors)
        
        external_ip = info.get("external_ip", "localhost")
        external_port = info.get("external_port", info["port"])
        
        logger.info(
            f"[{server_id}] Resolución de processor -> {pid} "
            f"(interno:{info['ip']}:{info['port']}, externo:{external_ip}:{external_port})"
        )
        
        return ProcessorResolveResponse(
            processor_id=pid,
            ip=info["ip"],
            port=info["port"],
            url=f"http://{info['ip']}:{info['port']}",
            external_ip=external_ip,
            external_port=external_port
        )


@app.get("/processor/list")
async def list_processors():
    """Lista todos los Processor Nodes registrados con su estado."""
    async with processor_servers_lock:
        now = datetime.now()
        processors = []
        
        for pid, info in processor_servers.items():
            last_hb = datetime.fromisoformat(info["last_heartbeat"])
            elapsed = (now - last_hb).total_seconds()
            
            # Usar external_ip detectada en el registro (no hardcodeada a localhost)
            external_ip = info.get("external_ip", "localhost")
            external_port = info.get("external_port", info["port"])
            
            processors.append({
                "processor_id": pid,
                "ip": info["ip"],
                "port": info["port"],
                "external_port": external_port,
                "external_ip": external_ip,
                "last_heartbeat": info["last_heartbeat"],
                "registered_at": info["registered_at"],
                "url": f"http://{info['ip']}:{info['port']}",
                "external_url": f"http://{external_ip}:{external_port}",
                "alive": elapsed < PROCESSOR_TIMEOUT,
                "seconds_since_heartbeat": int(elapsed)
            })
        
        return {
            "processors": processors,
            "total": len(processors),
            "active": sum(1 for p in processors if p["alive"]),
            "timestamp": now.isoformat()
        }


@app.get("/cluster/health")
async def cluster_health():
    """
    Endpoint de diagnóstico para monitorear el estado del cluster de Storage Nodes.
    Detecta y reporta problemas como split-brain (múltiples PRIMARY).
    """
    async with api_servers_lock:
        now = datetime.now()
        
        # Detectar múltiples PRIMARY
        primaries = detect_multiple_primaries()
        has_split_brain = len(primaries) > 1
        
        # Información detallada de todos los nodos
        nodes_info = []
        for sid, info in api_servers.items():
            last_hb = datetime.fromisoformat(info["last_heartbeat"])
            elapsed = (now - last_hb).total_seconds()
            is_alive = elapsed < API_SERVER_TIMEOUT
            
            nodes_info.append({
                "server_id": sid,
                "role": info.get("role"),
                "ip": info.get("ip"),
                "port": info.get("port"),
                "alive": is_alive,
                "seconds_since_heartbeat": round(elapsed, 2),
                "primary_epoch": info.get("primary_epoch"),
                "lease_expires_at": info.get("lease_expires_at"),
                "registered_at": info.get("registered_at"),
                "last_heartbeat": info.get("last_heartbeat"),
            })
        
        # Estadísticas
        total_nodes = len(nodes_info)
        active_nodes = sum(1 for n in nodes_info if n["alive"])
        primary_count = len(primaries)
        backup_count = sum(1 for n in nodes_info if n["role"] == "BACKUP")
        
        # Determinar el líder actual (o el que debería ser)
        current_leader = None
        if primaries:
            current_leader = elect_single_leader(primaries)
        
        return {
            "cluster_status": "split_brain" if has_split_brain else "healthy",
            "split_brain_detected": has_split_brain,
            "multiple_primaries": primaries if has_split_brain else None,
            "current_leader": current_leader,
            "global_primary_epoch": primary_epoch,
            "statistics": {
                "total_nodes": total_nodes,
                "active_nodes": active_nodes,
                "primary_count": primary_count,
                "backup_count": backup_count,
            },
            "nodes": nodes_info,
            "dns_server_id": server_id,
            "dns_role": server_role,
            "timestamp": now.isoformat(),
            "last_split_brain_check": datetime.fromtimestamp(last_split_brain_check).isoformat() if last_split_brain_check > 0 else None,
        }


@app.post("/notify-new-primary")
async def list_processors():
    """Lista todos los Processor Nodes registrados con su estado."""
    async with processor_servers_lock:
        now = datetime.now()
        processors = []
        
        for pid, info in processor_servers.items():
            last_hb = datetime.fromisoformat(info["last_heartbeat"])
            elapsed = (now - last_hb).total_seconds()
            
            processors.append({
                "processor_id": pid,
                "ip": info["ip"],
                "port": info["port"],
                "last_heartbeat": info["last_heartbeat"],
                "registered_at": info["registered_at"],
                "url": f"http://{info['ip']}:{info['port']}",
                "alive": elapsed < PROCESSOR_TIMEOUT,
                "seconds_since_heartbeat": int(elapsed)
            })
        
        return {
            "processors": processors,
            "total": len(processors),
            "active": sum(1 for p in processors if p["alive"]),
            "timestamp": now.isoformat()
        }


@app.post("/election")
async def handle_election_message(data: dict):
    """
    Maneja mensaje ELECTION del algoritmo de bully.
    Responde OK y luego inicia mi propia elección si tengo mayor ID.
    """
    candidate_id = data.get("candidate_id")
    
    logger.info(f"[{server_id}] Recibido ELECTION de {candidate_id}")
    
    # Si mi ID es mayor, respondo OK e inicio mi propia elección
    if server_id > candidate_id:
        logger.info(f"[{server_id}] Mi ID es mayor que {candidate_id}. Respondiendo OK e iniciando elección...")
        
        # Iniciar mi propia elección en background
        asyncio.create_task(start_election())
        
        return {"status": "ok", "server_id": server_id}
    else:
        # Mi ID es menor, no participo
        logger.debug(f"[{server_id}] Mi ID es menor que {candidate_id}. No respondo.")
        return {"status": "declined", "server_id": server_id}


@app.post("/coordinator")
async def handle_coordinator_announcement(data: dict):
    """
    Maneja anuncio de nuevo coordinador (PRIMARY) del algoritmo de bully.
    """
    global server_role, current_primary_url, primary_since
    
    coordinator_id = data.get("coordinator_id")
    coordinator_url = data.get("coordinator_url")
    coordinator_primary_since = data.get("primary_since")
    
    logger.info(f"[{server_id}] *** COORDINATOR RECIBIDO: {coordinator_id} es el PRIMARY ***")
    
    # Si yo era primary, ceder inmediatamente
    if server_role == "primary" and coordinator_id != server_id:
        # Verificar que el coordinador tenga mayor ID (validación)
        if coordinator_id > server_id:
            logger.warning(f"[{server_id}] Cediendo PRIMARY a {coordinator_id} (mayor ID)")
            server_role = "backup"
            primary_since = None
        else:
            # El coordinador tiene menor ID, esto es incorrecto
            logger.error(f"[{server_id}] Coordinador {coordinator_id} tiene MENOR ID. Iniciando elección...")
            asyncio.create_task(start_election())
            return {"status": "rejected", "server_id": server_id}
    
    # Actualizar estado del coordinador
    if coordinator_id in cluster_state:
        cluster_state[coordinator_id]["role"] = "primary"
        cluster_state[coordinator_id]["primary_since"] = coordinator_primary_since
    
    # Si no soy el coordinador, actualizar mi estado
    if coordinator_id != server_id:
        server_role = "backup"
        primary_since = None
        current_primary_url = coordinator_url
        
        # Actualizar mi entrada
        if server_id in cluster_state:
            cluster_state[server_id]["role"] = "backup"
            cluster_state[server_id]["primary_since"] = None
        
        # Sincronizar inmediatamente
        await sync_from_primary()
    
    return {"status": "acknowledged", "server_id": server_id}


@app.post("/notify-new-primary")
async def receive_new_primary_notification(data: dict):
    """DEPRECATED: Redirigir a /coordinator para compatibilidad."""
    return await handle_coordinator_announcement({
        "coordinator_id": data.get("new_primary_id"),
        "coordinator_url": data.get("new_primary_url"),
        "primary_since": data.get("primary_since"),
        "timestamp": data.get("timestamp")
    })


# ============================================================================
# DESCUBRIMIENTO DINÁMICO
# ============================================================================

def get_my_ip() -> Optional[str]:
    """
    Obtiene la IP de este contenedor EN LA RED OVERLAY de Docker.
    
    En redes overlay, la IP que nos interesa es la que otros contenedores
    pueden usar para contactarnos. Usamos el hostname del contenedor
    que Docker resuelve correctamente dentro de la red overlay.
    """
    try:
        # Primero intentamos resolver nuestro propio hostname
        # Docker DNS resolverá esto a nuestra IP en la red overlay
        hostname = socket.gethostname()
        ip = socket.gethostbyname(hostname)
        logger.debug(f"[{server_id}] IP obtenida via hostname '{hostname}': {ip}")
        return ip
    except socket.gaierror:
        pass
    
    try:
        # Fallback: intentar resolver el server_id directamente
        # En Docker con network-alias, esto debería funcionar
        ip = socket.gethostbyname(server_id)
        logger.debug(f"[{server_id}] IP obtenida via server_id: {ip}")
        return ip
    except socket.gaierror:
        pass
    
    try:
        # Último fallback: técnica de socket UDP (menos confiable en overlay)
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # Conectamos al DNS alias en lugar de una IP externa
        s.connect((DNS_ALIAS, dns_port))
        ip = s.getsockname()[0]
        s.close()
        logger.debug(f"[{server_id}] IP obtenida via socket UDP: {ip}")
        return ip
    except Exception as e:
        logger.warning(f"[{server_id}] No se pudo obtener IP: {e}")
        return None


async def discover_via_dns_alias():
    """
    Descubre otros servidores DNS usando el alias común de Docker DNS.
    Docker DNS retorna todas las IPs que comparten el alias.
    """
    global known_dns_ips, cluster_state
    
    logger.debug(f"[{server_id}] Descubriendo servidores via alias '{DNS_ALIAS}'...")
    
    try:
        # getaddrinfo retorna todas las IPs asociadas al alias
        results = socket.getaddrinfo(DNS_ALIAS, dns_port, socket.AF_INET, socket.SOCK_STREAM)
        discovered_ips = set(result[4][0] for result in results)
        
        logger.info(f"[{server_id}] IPs descubiertas via alias: {discovered_ips}")
        
        # Contactar cada IP descubierta (excepto la nuestra)
        for ip in discovered_ips:
            if ip == my_ip:
                continue
                
            if ip in known_dns_ips:
                # Ya conocemos este servidor, solo actualizar estado
                await check_server_health(ip)
            else:
                # Nuevo servidor, registrarse mutuamente
                await register_with_server(ip)
                
    except socket.gaierror as e:
        logger.debug(f"[{server_id}] No se pudo resolver alias '{DNS_ALIAS}': {e}")
    except Exception as e:
        logger.error(f"[{server_id}] Error en descubrimiento: {e}")


async def register_with_server(ip: str):
    """Registra este servidor con otro servidor DNS"""
    try:
        url = f"http://{ip}:{dns_port}"
        
        async with httpx.AsyncClient(timeout=3.0) as client:
            # Registrarme con el otro servidor
            response = await client.post(f"{url}/register", json={
                "server_id": server_id,
                "ip": my_ip,
                "hostname": my_hostname,
                "role": server_role,
                "primary_since": primary_since
            })
            
            if response.status_code == 200:
                data = response.json()
                other_id = data.get("my_id")
                other_ip = data.get("my_ip")
                other_role = data.get("my_role")
                other_primary_since = data.get("primary_since")
                
                # Agregar al estado del clúster
                cluster_state[other_id] = {
                    "url": url,
                    "ip": ip,
                    "hostname": other_id,
                    "healthy": True,
                    "role": other_role,
                    "primary_since": other_primary_since,
                    "last_seen": datetime.now().isoformat()
                }
                known_dns_ips.add(ip)
                
                logger.info(f"[{server_id}] Registrado con {other_id} ({ip}), rol={other_role}")
                
    except Exception as e:
        logger.warning(f"[{server_id}] No se pudo registrar con {ip}: {e}")


async def check_server_health(ip: str):
    """Verifica el estado de un servidor DNS conocido"""
    try:
        url = f"http://{ip}:{dns_port}"
        
        async with httpx.AsyncClient(timeout=3.0) as client:
            response = await client.get(f"{url}/health")
            
            if response.status_code == 200:
                data = response.json()
                sid = data.get("server_id")
                
                if sid in cluster_state:
                    cluster_state[sid]["healthy"] = True
                    cluster_state[sid]["role"] = data.get("role", "unknown")
                    cluster_state[sid]["primary_since"] = data.get("primary_since")
                    cluster_state[sid]["last_seen"] = datetime.now().isoformat()
                else:
                    # Servidor nuevo, registrar
                    cluster_state[sid] = {
                        "url": url,
                        "ip": ip,
                        "hostname": sid,
                        "healthy": True,
                        "role": data.get("role", "unknown"),
                        "primary_since": data.get("primary_since"),
                        "last_seen": datetime.now().isoformat()
                    }
                    known_dns_ips.add(ip)
                    
                return True
                
    except Exception as e:
        # Marcar como no healthy pero mantener last_seen original para calcular timeout
        for sid, info in list(cluster_state.items()):
            if info.get("ip") == ip:
                cluster_state[sid]["healthy"] = False
                logger.debug(f"[{server_id}] Servidor DNS {sid} ({ip}) no responde")
                break
        return False
    
    return False


async def cleanup_dead_dns_servers():
    """
    Elimina del registro los servidores DNS que han excedido el timeout.
    Un servidor se considera muerto si no ha respondido en DNS_SERVER_TIMEOUT segundos.
    """
    global known_dns_ips
    
    now = datetime.now()
    servers_to_remove = []
    
    for sid, info in list(cluster_state.items()):
        # No eliminarnos a nosotros mismos
        if sid == server_id:
            continue
        
        # Solo considerar servidores marcados como no healthy
        if info.get("healthy", True):
            continue
        
        # Verificar tiempo desde última respuesta exitosa
        last_seen_str = info.get("last_seen")
        if not last_seen_str:
            continue
            
        try:
            last_seen = datetime.fromisoformat(last_seen_str)
            elapsed = (now - last_seen).total_seconds()
            
            if elapsed > DNS_SERVER_TIMEOUT:
                servers_to_remove.append((sid, info.get("ip")))
                
        except (ValueError, TypeError) as e:
            logger.debug(f"[{server_id}] Error parseando last_seen de {sid}: {e}")
    
    # Eliminar servidores muertos
    for sid, ip in servers_to_remove:
        logger.warning(f"[{server_id}] Eliminando servidor DNS {sid} ({ip}) del registro - timeout excedido ({DNS_SERVER_TIMEOUT}s)")
        
        # Eliminar del cluster_state
        if sid in cluster_state:
            del cluster_state[sid]
        
        # Eliminar de known_dns_ips
        if ip and ip in known_dns_ips:
            known_dns_ips.discard(ip)
    
    if servers_to_remove:
        logger.info(f"[{server_id}] Limpieza completada: {len(servers_to_remove)} servidor(es) DNS eliminado(s). Clúster actual: {list(cluster_state.keys())}")


async def determine_role():
    """
    Determina el rol usando algoritmo de bully mejorado.
    Detecta múltiples primarios y fuerza elección.
    """
    global server_role, current_primary_url, primary_since, election_in_progress, last_election_time
    
    # Contar primarios activos
    active_primaries = []
    for sid, info in cluster_state.items():
        if info.get("role") == "primary" and info.get("healthy"):
            active_primaries.append((sid, info))
    
    # CASO 1: Múltiples primarios (split-brain)
    if len(active_primaries) > 1:
        logger.warning(f"[{server_id}] *** SPLIT-BRAIN DETECTADO: {len(active_primaries)} DNS PRIMARY ***")
        # Forzar elección inmediatamente
        await start_election()
        return
    
    # CASO 2: Hay exactamente un primario y no soy yo
    if len(active_primaries) == 1:
        primary_id, primary_info = active_primaries[0]
        
        if primary_id != server_id:
            # Hay otro primario, debo ser backup
            if server_role == "primary":
                logger.warning(f"[{server_id}] Detecté otro PRIMARY ({primary_id}). Iniciando elección...")
                await start_election()
            else:
                # Ya soy backup, todo bien
                if server_role != "backup":
                    logger.info(f"[{server_id}] Primario activo: {primary_id}. Configurándome como BACKUP")
                    server_role = "backup"
                    primary_since = None
                current_primary_url = primary_info.get("url")
                await sync_from_primary()
        else:
            # Soy el único primario, mantener
            pass
    
    # CASO 3: No hay primario
    elif len(active_primaries) == 0:
        if server_role != "primary":
            logger.warning(f"[{server_id}] No hay primario. Iniciando elección...")
            await start_election()


async def start_election():
    """
    Inicia el algoritmo de bully para elección de líder DNS.
    
    Algoritmo:
    1. Enviar ELECTION a todos los servidores con ID mayor
    2. Si alguno responde OK, esperar que ellos resuelvan
    3. Si nadie responde, me convierto en PRIMARY y anuncio COORDINATOR
    """
    global election_in_progress, server_role, primary_since, current_primary_url, last_election_time
    
    # Cooldown para evitar elecciones excesivas
    now = time.time()
    if election_in_progress:
        logger.debug(f"[{server_id}] Elección ya en progreso, saltando...")
        return
    
    if now - last_election_time < ELECTION_COOLDOWN:
        logger.debug(f"[{server_id}] En cooldown de elección, saltando...")
        return
    
    election_in_progress = True
    last_election_time = now
    
    logger.info(f"[{server_id}] *** INICIANDO ELECCIÓN (BULLY ALGORITHM) ***")
    
    try:
        # Obtener servidores con ID mayor (lexicográficamente)
        higher_servers = []
        for sid, info in cluster_state.items():
            if sid == server_id:
                continue
            if not info.get("healthy"):
                continue
            if sid > server_id:  # Comparación lexicográfica
                higher_servers.append((sid, info))
        
        if not higher_servers:
            # Soy el de mayor ID, me convierto en coordinador
            logger.info(f"[{server_id}] Soy el de mayor ID. Convirtiéndome en PRIMARY...")
            await become_primary()
            await announce_coordinator()
            return
        
        # Enviar mensaje ELECTION a servidores con ID mayor
        logger.info(f"[{server_id}] Enviando ELECTION a {len(higher_servers)} servidor(es) con ID mayor")
        responses = await send_election_messages(higher_servers)
        
        if not responses:
            # Nadie respondió, me convierto en coordinador
            logger.info(f"[{server_id}] Sin respuestas. Convirtiéndome en PRIMARY...")
            await become_primary()
            await announce_coordinator()
        else:
            # Alguien respondió, ellos manejarán la elección
            logger.info(f"[{server_id}] Recibí {len(responses)} respuesta(s). Esperando nuevo coordinador...")
            server_role = "backup"
            primary_since = None
            
            # Actualizar mi estado
            if server_id in cluster_state:
                cluster_state[server_id]["role"] = "backup"
                cluster_state[server_id]["primary_since"] = None
    
    finally:
        election_in_progress = False


async def send_election_messages(higher_servers: List[tuple]) -> List[str]:
    """
    Envía mensajes ELECTION a servidores con ID mayor.
    Retorna lista de server_ids que respondieron OK.
    """
    responses = []
    
    tasks = []
    for sid, info in higher_servers:
        url = info.get("url")
        if url:
            tasks.append(send_single_election(sid, url))
    
    if tasks:
        results = await asyncio.gather(*tasks, return_exceptions=True)
        responses = [r for r in results if r and not isinstance(r, Exception)]
    
    return responses


async def send_single_election(target_id: str, target_url: str) -> Optional[str]:
    """Envía mensaje ELECTION a un servidor específico."""
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            response = await client.post(f"{target_url}/election", json={
                "candidate_id": server_id,
                "timestamp": datetime.now().isoformat()
            })
            
            if response.status_code == 200:
                data = response.json()
                if data.get("status") == "ok":
                    logger.debug(f"[{server_id}] {target_id} respondió OK a ELECTION")
                    return target_id
    except Exception as e:
        logger.debug(f"[{server_id}] {target_id} no respondió a ELECTION: {e}")
    
    return None


async def become_primary():
    """Promueve este servidor a PRIMARY."""
    global server_role, primary_since, current_primary_url
    
    logger.warning(f"[{server_id}] *** PROMOCIÓN A PRIMARY ***")
    server_role = "primary"
    primary_since = time.time()
    current_primary_url = None
    
    # Actualizar mi entrada en cluster_state
    if server_id in cluster_state:
        cluster_state[server_id]["role"] = "primary"
        cluster_state[server_id]["primary_since"] = primary_since


async def announce_coordinator():
    """Anuncia que soy el nuevo coordinador (PRIMARY) a todos."""
    logger.info(f"[{server_id}] *** ANUNCIANDO COORDINATOR A TODOS ***")
    
    tasks = []
    for sid, info in cluster_state.items():
        if sid == server_id or not info.get("healthy"):
            continue
        
        url = info.get("url")
        if url:
            tasks.append(send_coordinator_announcement(sid, url))
    
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    
    logger.info(f"[{server_id}] *** COORDINATOR ANUNCIADO ***")


async def send_coordinator_announcement(target_id: str, target_url: str):
    """Envía anuncio de coordinador a un servidor."""
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            await client.post(f"{target_url}/coordinator", json={
                "coordinator_id": server_id,
                "coordinator_url": f"http://{my_ip}:{dns_port}",
                "primary_since": primary_since,
                "timestamp": datetime.now().isoformat()
            })
            logger.debug(f"[{server_id}] Anuncio COORDINATOR enviado a {target_id}")
    except Exception as e:
        logger.debug(f"[{server_id}] Error enviando COORDINATOR a {target_id}: {e}")


async def notify_promotion():
    """DEPRECATED: Usar announce_coordinator en su lugar."""
    await announce_coordinator()


# ============================================================================
# LEADER ELECTION & SPLIT-BRAIN RESOLUTION
# ============================================================================

def detect_multiple_primaries() -> List[str]:
    """
    Detecta si hay múltiples nodos storage con rol PRIMARY.
    Retorna la lista de server_ids que se creen PRIMARY.
    """
    primaries = []
    for sid, info in api_servers.items():
        if info.get("role") == "PRIMARY":
            primaries.append(sid)
    
    if len(primaries) > 1:
        logger.warning(
            f"[{server_id}] *** SPLIT-BRAIN DETECTADO: {len(primaries)} nodos PRIMARY: {primaries} ***"
        )
    
    return primaries


def elect_single_leader(primaries: List[str]) -> str:
    """
    Elige un único líder entre múltiples PRIMARY usando criterios determinísticos:
    1. PRIMARY con epoch más alto (última promoción válida)
    2. Si hay empate, el registrado primero (registered_at más antiguo)
    3. Si aún hay empate, ID lexicográfico menor (determinista)
    
    Args:
        primaries: Lista de server_ids que se creen PRIMARY
        
    Returns:
        El server_id del líder elegido
    """
    if not primaries:
        return None
    
    if len(primaries) == 1:
        return primaries[0]
    
    # Construir lista de candidatos con sus métricas
    candidates = []
    for sid in primaries:
        info = api_servers.get(sid)
        if not info:
            continue
        
        candidates.append({
            "server_id": sid,
            "primary_epoch": info.get("primary_epoch", 0) or 0,
            "registered_at": info.get("registered_at", datetime.now().isoformat()),
            "last_heartbeat": info.get("last_heartbeat", ""),
        })
    
    if not candidates:
        return None
    
    # Ordenar por:
    # 1. Epoch descendente (mayor epoch gana)
    # 2. registered_at ascendente (más antiguo gana)
    # 3. server_id ascendente (lexicográfico)
    candidates.sort(
        key=lambda x: (
            -x["primary_epoch"],  # Mayor epoch primero
            x["registered_at"],   # Más antiguo primero
            x["server_id"]        # ID menor primero
        )
    )
    
    winner = candidates[0]
    logger.info(
        f"[{server_id}] Líder elegido: {winner['server_id']} "
        f"(epoch={winner['primary_epoch']}, registered={winner['registered_at']})"
    )
    
    return winner["server_id"]


async def resolve_split_brain():
    """
    Resuelve situaciones de split-brain donde múltiples nodos se creen PRIMARY.
    
    Algoritmo:
    1. Detectar múltiples PRIMARY
    2. Elegir un único líder usando criterios determinísticos
    3. Degradar (fence) a los perdedores a BACKUP
    4. Notificar a los nodos afectados mediante propagación
    
    Esta función se ejecuta periódicamente y en eventos críticos (heartbeat, sync).
    """
    global last_split_brain_check, primary_epoch
    
    now = time.time()
    
    # Throttling: no verificar muy frecuentemente
    if now - last_split_brain_check < SPLIT_BRAIN_CHECK_INTERVAL:
        return
    
    last_split_brain_check = now
    
    async with api_servers_lock:
        # Detectar múltiples PRIMARY
        primaries = detect_multiple_primaries()
        
        if len(primaries) <= 1:
            # No hay conflicto
            return
        
        logger.warning(
            f"[{server_id}] *** RESOLVIENDO SPLIT-BRAIN: {len(primaries)} PRIMARY detectados ***"
        )
        
        # Elegir líder único
        winner_id = elect_single_leader(primaries)
        
        if not winner_id:
            logger.error(f"[{server_id}] No se pudo elegir líder en split-brain resolution")
            return
        
        # Degradar perdedores a BACKUP
        losers = [sid for sid in primaries if sid != winner_id]
        
        for loser_id in losers:
            if loser_id not in api_servers:
                continue
            
            logger.warning(
                f"[{server_id}] *** FENCING: Degradando {loser_id} de PRIMARY a BACKUP ***"
            )
            
            # Cambiar rol a BACKUP
            api_servers[loser_id]["role"] = "BACKUP"
            api_servers[loser_id]["primary_epoch"] = None
            api_servers[loser_id]["lease_expires_at"] = None
            
            # Propagar cambio a otros DNS (sin await para no bloquear)
            asyncio.create_task(
                propagate_service_registration(
                    "api_server",
                    loser_id,
                    dict(api_servers[loser_id])
                )
            )
        
        # Asegurar que el ganador tenga epoch actualizado
        if winner_id in api_servers:
            winner_epoch = api_servers[winner_id].get("primary_epoch", 0)
            
            # Incrementar epoch global si es necesario
            if winner_epoch is None or winner_epoch < primary_epoch:
                primary_epoch += 1
                api_servers[winner_id]["primary_epoch"] = primary_epoch
                
                # Renovar lease del ganador
                now_dt = datetime.now()
                lease_expires = now_dt.timestamp() + LEASE_DURATION
                api_servers[winner_id]["lease_expires_at"] = datetime.fromtimestamp(lease_expires).isoformat()
                
                logger.info(
                    f"[{server_id}] Ganador {winner_id} actualizado con epoch={primary_epoch}"
                )
            else:
                # El epoch del ganador es el más alto, actualizamos el global
                primary_epoch = winner_epoch
            
            # Propagar estado del ganador
            asyncio.create_task(
                propagate_service_registration(
                    "api_server",
                    winner_id,
                    dict(api_servers[winner_id])
                )
            )
        
        logger.warning(
            f"[{server_id}] *** SPLIT-BRAIN RESUELTO: {winner_id} es el PRIMARY único, "
            f"{len(losers)} nodo(s) degradado(s) ***"
        )


# ============================================================================
# SINCRONIZACIÓN
# ============================================================================

async def propagate_service_registration(service_type: str, service_id: str, service_info: dict):
    """
    Propaga el registro de un servicio (processor o api_server) a todos los otros DNS.
    Esto asegura que si un servicio se registra en un DNS, todos los demás lo conozcan inmediatamente.
    """
    logger.debug(f"[{server_id}] Propagando registro de {service_type}/{service_id} a otros DNS...")
    
    # Construir los datos de sync
    sync_data = {
        "cache": {},
        "timestamp": datetime.now().isoformat(),
        "source_id": server_id,
        "api_servers": {},
        "processor_servers": {}
    }
    
    if service_type == "processor":
        sync_data["processor_servers"] = {service_id: service_info}
    elif service_type == "api_server":
        sync_data["api_servers"] = {service_id: service_info}
    
    # Enviar a todos los otros DNS conocidos
    for sid, info in cluster_state.items():
        if sid == server_id:
            continue
            
        if not info.get("healthy"):
            continue
            
        url = info.get("url")
        if not url:
            continue
            
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                response = await client.post(f"{url}/sync", json=sync_data)
                if response.status_code == 200:
                    logger.debug(f"[{server_id}] Registro de {service_type}/{service_id} propagado a {sid}")
        except Exception as e:
            logger.debug(f"[{server_id}] Error propagando registro a {sid}: {e}")


async def propagate_to_backups(hostname: str, ip: str):
    """Propaga una resolución a los servidores de backup"""
    if server_role != "primary":
        return
        
    logger.debug(f"[{server_id}] Propagando '{hostname}' a backups...")
    
    for sid, info in cluster_state.items():
        if sid == server_id or info.get("role") == "primary":
            continue
            
        if not info.get("healthy"):
            continue
            
        url = info.get("url")
        if not url:
            continue
            
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                sync_data = {
                    "cache": {hostname: dns_cache[hostname]},
                    "timestamp": datetime.now().isoformat(),
                    "source_id": server_id
                }
                response = await client.post(f"{url}/sync", json=sync_data)
                if response.status_code == 200:
                    logger.debug(f"[{server_id}] Propagación a {sid}: OK")
        except Exception as e:
            logger.error(f"[{server_id}] Error propagando a {sid}: {e}")


async def cleanup_dead_api_servers():
    """Elimina servidores API (storage nodes) que han excedido el timeout"""
    async with api_servers_lock:
        now = datetime.now()
        servers_to_remove = []
        
        for sid, info in api_servers.items():
            last_hb_str = info.get("last_heartbeat")
            if not last_hb_str:
                continue
            
            try:
                last_hb = datetime.fromisoformat(last_hb_str)
                elapsed = (now - last_hb).total_seconds()
                
                if elapsed > API_SERVER_TIMEOUT:
                    servers_to_remove.append(sid)
                    
            except (ValueError, TypeError) as e:
                logger.debug(f"[{server_id}] Error parseando last_heartbeat de {sid}: {e}")
        
        # Eliminar servidores muertos
        for sid in servers_to_remove:
            info = api_servers[sid]
            logger.warning(
                f"[{server_id}] Eliminando storage node {sid} ({info.get('ip')}:{info.get('port')}) "
                f"del registro - sin heartbeat por >{API_SERVER_TIMEOUT}s (role: {info.get('role')})"
            )
            del api_servers[sid]
        
        if servers_to_remove:
            logger.info(
                f"[{server_id}] Limpieza completada: {len(servers_to_remove)} storage node(s) eliminado(s). "
                f"Nodos activos: {list(api_servers.keys())}"
            )


async def cleanup_dead_processor_servers():
    """Elimina processor nodes que han excedido el timeout"""
    async with processor_servers_lock:
        now = datetime.now()
        processors_to_remove = []
        
        for pid, info in processor_servers.items():
            last_hb_str = info.get("last_heartbeat")
            if not last_hb_str:
                continue
            
            try:
                last_hb = datetime.fromisoformat(last_hb_str)
                elapsed = (now - last_hb).total_seconds()
                
                if elapsed > PROCESSOR_TIMEOUT:
                    processors_to_remove.append(pid)
                    
            except (ValueError, TypeError) as e:
                logger.debug(f"[{server_id}] Error parseando last_heartbeat de {pid}: {e}")
        
        # Eliminar processors muertos
        for pid in processors_to_remove:
            info = processor_servers[pid]
            logger.warning(
                f"[{server_id}] Eliminando processor node {pid} ({info.get('ip')}:{info.get('port')}) "
                f"del registro - sin heartbeat por >{PROCESSOR_TIMEOUT}s"
            )
            del processor_servers[pid]
        
        if processors_to_remove:
            logger.info(
                f"[{server_id}] Limpieza completada: {len(processors_to_remove)} processor(s) eliminado(s). "
                f"Processors activos: {list(processor_servers.keys())}"
            )


async def sync_from_primary():
    """Sincroniza el estado completo desde el primario (cache, api_servers, processor_servers)"""
    global current_primary_url
    
    if server_role == "primary" or not current_primary_url:
        return
        
    try:
        logger.info(f"[{server_id}] Sincronizando desde primario: {current_primary_url}")
        
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(f"{current_primary_url}/cache")
            if response.status_code == 200:
                data = response.json()
                
                # Sincronizar cache DNS
                for hostname, entry in data.get("cache", {}).items():
                    dns_cache[hostname] = entry
                
                # Sincronizar api_servers (storage nodes)
                async with api_servers_lock:
                    for sid, info in data.get("api_servers", {}).items():
                        if sid not in api_servers:
                            api_servers[sid] = info
                        else:
                            existing_hb = api_servers[sid].get("last_heartbeat", "")
                            incoming_hb = info.get("last_heartbeat", "")
                            if incoming_hb > existing_hb:
                                api_servers[sid] = info
                
                # Sincronizar processor_servers
                async with processor_servers_lock:
                    for pid, info in data.get("processor_servers", {}).items():
                        if pid not in processor_servers:
                            processor_servers[pid] = info
                        else:
                            existing_hb = processor_servers[pid].get("last_heartbeat", "")
                            incoming_hb = info.get("last_heartbeat", "")
                            if incoming_hb > existing_hb:
                                processor_servers[pid] = info
                
                sync_status["last_sync"] = datetime.now().isoformat()
                sync_status["sync_count"] += 1
                
                logger.info(f"[{server_id}] Sincronización completada. Cache: {len(dns_cache)}, API servers: {len(api_servers)}, Processors: {len(processor_servers)}")
    except Exception as e:
        logger.error(f"[{server_id}] Error en sincronización: {e}")


# ============================================================================
# LOOPS EN BACKGROUND
# ============================================================================

async def discovery_loop():
    """Loop de descubrimiento periódico de servidores DNS"""
    while True:
        try:
            await asyncio.sleep(DISCOVERY_INTERVAL)
            await discover_via_dns_alias()
        except Exception as e:
            logger.error(f"[{server_id}] Error en discovery loop: {e}")


async def health_check_loop():
    """Loop de health checks y determinación de rol"""
    while True:
        try:
            await asyncio.sleep(HEALTH_CHECK_INTERVAL)
            
            # Verificar salud de todos los servidores conocidos
            for sid, info in list(cluster_state.items()):
                if sid == server_id:
                    continue
                ip = info.get("ip")
                if ip:
                    await check_server_health(ip)
            
            # Limpiar servidores DNS que han excedido el timeout
            await cleanup_dead_dns_servers()
            
            # Limpiar storage nodes muertos
            await cleanup_dead_api_servers()
            
            # Limpiar processor nodes muertos
            await cleanup_dead_processor_servers()
            
            # Determinar mi rol basándome en el estado actual
            await determine_role()
            
        except Exception as e:
            logger.error(f"[{server_id}] Error en health check loop: {e}")


async def sync_loop():
    """Loop de sincronización periódica (solo backups)"""
    while True:
        try:
            await asyncio.sleep(SYNC_INTERVAL)
            
            if server_role == "backup" and current_primary_url:
                if not sync_status["is_syncing"]:
                    sync_status["is_syncing"] = True
                    await sync_from_primary()
                    sync_status["is_syncing"] = False
        except Exception as e:
            logger.error(f"[{server_id}] Error en sync loop: {e}")
            sync_status["is_syncing"] = False


async def dns_split_brain_monitor_loop():
    """
    Loop de monitoreo y resolución de split-brain en servidores DNS.
    Detecta múltiples PRIMARY DNS y fuerza elección mediante bully algorithm.
    """
    logger.info(f"[{server_id}] Iniciando monitor de split-brain DNS (intervalo: {SPLIT_BRAIN_CHECK_INTERVAL}s)")
    
    while True:
        try:
            await asyncio.sleep(SPLIT_BRAIN_CHECK_INTERVAL)
            
            # Contar cuántos DNS se creen PRIMARY
            dns_primaries = []
            for sid, info in cluster_state.items():
                if info.get("role") == "primary" and info.get("healthy"):
                    dns_primaries.append(sid)
            
            # Si hay múltiples primarios, forzar elección
            if len(dns_primaries) > 1:
                logger.warning(
                    f"[{server_id}] *** DNS SPLIT-BRAIN: {len(dns_primaries)} PRIMARY detectados: {dns_primaries} ***"
                )
                # Forzar elección inmediatamente
                await start_election()
            
        except Exception as e:
            logger.error(f"[{server_id}] Error en DNS split-brain monitor loop: {e}")


async def split_brain_monitor_loop():
    """
    Loop de monitoreo y resolución automática de split-brain para storage nodes.
    Ejecuta verificaciones periódicas independientes de otros eventos.
    """
    logger.info(f"[{server_id}] Iniciando monitor de split-brain STORAGE (intervalo: {SPLIT_BRAIN_CHECK_INTERVAL}s)")
    
    while True:
        try:
            await asyncio.sleep(SPLIT_BRAIN_CHECK_INTERVAL)
            
            # Forzar verificación sin throttling (ya que el loop controla el intervalo)
            global last_split_brain_check
            last_split_brain_check = 0  # Reset para forzar verificación
            
            await resolve_split_brain()
            
        except Exception as e:
            logger.error(f"[{server_id}] Error en split-brain monitor loop: {e}")


# ============================================================================
# STARTUP
# ============================================================================

@app.on_event("startup")
async def startup_event():
    """Inicialización del servicio DNS HA"""
    global my_ip, my_hostname, server_role, primary_since
    
    logger.info(f"[{server_id}] ========================================")
    logger.info(f"[{server_id}] Iniciando DNS Service HA v2.2")
    logger.info(f"[{server_id}] Server ID: {server_id}")
    logger.info(f"[{server_id}] DNS Alias: {DNS_ALIAS}")
    logger.info(f"[{server_id}] Puerto: {dns_port}")
    logger.info(f"[{server_id}] Health Check Interval: {HEALTH_CHECK_INTERVAL}s")
    logger.info(f"[{server_id}] Discovery Interval: {DISCOVERY_INTERVAL}s")
    logger.info(f"[{server_id}] Sync Interval: {SYNC_INTERVAL}s")
    logger.info(f"[{server_id}] DNS Server Timeout: {DNS_SERVER_TIMEOUT}s")
    logger.info(f"[{server_id}] ========================================")
    
    # Obtener mi IP
    my_ip = get_my_ip()
    my_hostname = socket.gethostname()
    logger.info(f"[{server_id}] Mi IP: {my_ip}, Hostname: {my_hostname}")
    
    # Registrarme a mí mismo en el cluster_state
    cluster_state[server_id] = {
        "url": f"http://{my_ip}:{dns_port}",
        "ip": my_ip,
        "hostname": my_hostname,
        "healthy": True,
        "role": "unknown",
        "primary_since": None,
        "last_seen": datetime.now().isoformat()
    }
    known_dns_ips.add(my_ip)
    
    # Esperar un poco para que Docker DNS esté listo
    await asyncio.sleep(2)
    
    # Descubrir otros servidores
    await discover_via_dns_alias()
    
    # Determinar rol inicial
    await determine_role()
    
    # Actualizar mi rol en cluster_state
    cluster_state[server_id]["role"] = server_role
    cluster_state[server_id]["primary_since"] = primary_since
    
    # Iniciar loops en background
    asyncio.create_task(discovery_loop())
    asyncio.create_task(health_check_loop())
    asyncio.create_task(sync_loop())
    asyncio.create_task(dns_split_brain_monitor_loop())  # Monitor de split-brain para DNS
    asyncio.create_task(split_brain_monitor_loop())  # Monitor de split-brain para storage nodes
    
    logger.info(f"[{server_id}] DNS Service HA iniciado. Rol: {server_role}")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=dns_port)
