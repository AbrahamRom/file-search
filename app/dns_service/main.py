import socket
import logging
import os
import time
import asyncio
from typing import Dict, Optional, List, Set
from datetime import datetime
from fastapi import FastAPI, HTTPException, BackgroundTasks
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


class ProcessorHeartbeatRequest(BaseModel):
    processor_id: str


class ProcessorResolveResponse(BaseModel):
    processor_id: str
    ip: str
    port: int
    url: str


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
async def receive_sync(sync_data: SyncData):
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
    El primer servidor en registrarse es el PRIMARY.
    El registro se propaga automáticamente a todos los otros DNS.
    """
    async with api_servers_lock:
        now = datetime.now().isoformat()
        
        # Verificar si ya hay un PRIMARY activo
        current_primary = None
        for sid, info in api_servers.items():
            if info["role"] == "PRIMARY":
                # Verificar si el PRIMARY está vivo
                last_hb = datetime.fromisoformat(info["last_heartbeat"])
                elapsed = (datetime.now() - last_hb).total_seconds()
                if elapsed < API_SERVER_TIMEOUT:
                    current_primary = sid
                    break
        
        # Asignar rol y epoch/lease
        global primary_epoch
        if current_primary is None:
            assigned_role = "PRIMARY"
            primary_epoch += 1
            lease_expires = datetime.now().timestamp() + LEASE_DURATION
            logger.info(f"[{server_id}] Servidor API {request.server_id} registrado como PRIMARY (epoch={primary_epoch})")
        else:
            assigned_role = "BACKUP"
            lease_expires = None
            logger.info(f"[{server_id}] Servidor API {request.server_id} registrado como BACKUP (PRIMARY: {current_primary})")
        
        # Registrar servidor
        server_info = {
            "ip": request.ip,
            "port": request.port,
            "role": assigned_role,
            "last_heartbeat": now,
            "registered_at": now,
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
        
        return {
            "status": "registered",
            "assigned_role": assigned_role,
            "server_id": request.server_id,
            "primary_info": primary_info,
            "total_servers": len(api_servers)
        }


@app.post("/server/heartbeat")
async def api_server_heartbeat(request: APIServerHeartbeatRequest, background_tasks: BackgroundTasks):
    """
    Recibe heartbeat de un servidor API y verifica/actualiza su rol.
    Maneja la promoción de BACKUPs a PRIMARY si es necesario.
    """
    async with api_servers_lock:
        if request.server_id not in api_servers:
            raise HTTPException(status_code=404, detail="Servidor no registrado")
        
        now = datetime.now()
        now_iso = now.isoformat()
        
        # Verificar estado del PRIMARY actual
        current_primary = None
        primary_alive = False
        
        for sid, info in api_servers.items():
            if info["role"] == "PRIMARY":
                current_primary = sid
                last_hb = datetime.fromisoformat(info["last_heartbeat"])
                elapsed = (now - last_hb).total_seconds()
                primary_alive = elapsed < API_SERVER_TIMEOUT
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

        # Si no hay PRIMARY o el PRIMARY está caído, promover
        if not primary_alive:
            if request.current_role == "BACKUP" or current_primary == request.server_id:
                # El servidor que reporta puede ser promovido
                # O el PRIMARY actual está reportando pero antes estaba caído
                
                # Elegir el BACKUP más antiguo para promover
                oldest_backup = None
                oldest_time = None
                
                for sid, info in api_servers.items():
                    if info["role"] == "BACKUP":
                        last_hb = datetime.fromisoformat(info["last_heartbeat"])
                        elapsed = (now - last_hb).total_seconds()
                        if elapsed < API_SERVER_TIMEOUT:
                            reg_time = datetime.fromisoformat(info["registered_at"])
                            if oldest_time is None or reg_time < oldest_time:
                                oldest_backup = sid
                                oldest_time = reg_time
                
                if current_primary and not primary_alive:
                    # Marcar el PRIMARY anterior como BACKUP (está caído pero por si vuelve)
                    logger.warning(f"[{server_id}] PRIMARY {current_primary} no responde. Timeout: {API_SERVER_TIMEOUT}s")
                    api_servers[current_primary]["role"] = "BACKUP"
                    affected_service_ids.add(current_primary)
                
                if oldest_backup:
                    # Promover el backup más antiguo con nuevo epoch
                    primary_epoch += 1
                    lease_expires = now.timestamp() + LEASE_DURATION
                    api_servers[oldest_backup]["role"] = "PRIMARY"
                    api_servers[oldest_backup]["primary_epoch"] = primary_epoch
                    api_servers[oldest_backup]["lease_expires_at"] = datetime.fromtimestamp(lease_expires).isoformat()
                    logger.warning(f"[{server_id}] *** FAILOVER: Promoviendo {oldest_backup} a PRIMARY (epoch={primary_epoch}) ***")

                    affected_service_ids.add(oldest_backup)
                    
                    if request.server_id == oldest_backup:
                        assigned_role = "PRIMARY"
                elif request.server_id == current_primary:
                    # El PRIMARY vuelve a estar activo
                    assigned_role = "PRIMARY"
                    api_servers[request.server_id]["role"] = "PRIMARY"
        
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
        
        return {
            "status": "ok",
            "assigned_role": assigned_role,
            "primary_info": primary_info,
            "timestamp": now_iso
        }


@app.get("/server/resolve", response_model=APIServerResolveResponse)
async def resolve_api_server():
    """
    Resuelve la IP del servidor API PRIMARY actual.
    Usado por los clientes para saber a qué servidor conectarse.
    """
    async with api_servers_lock:
        now = datetime.now()
        
        # Buscar el PRIMARY activo
        for sid, info in api_servers.items():
            if info["role"] == "PRIMARY":
                last_hb = datetime.fromisoformat(info["last_heartbeat"])
                elapsed = (now - last_hb).total_seconds()
                
                if elapsed < API_SERVER_TIMEOUT:
                    logger.info(f"[{server_id}] Resolución de servidor API -> {sid} ({info['ip']})")
                    return APIServerResolveResponse(
                        server_id=sid,
                        ip=info["ip"],
                        port=info["port"],
                        role="PRIMARY",
                        url=f"http://{info['ip']}:{info['port']}",
                        primary_epoch=info.get("primary_epoch"),
                        lease_expires_at=info.get("lease_expires_at")
                    )
        
        # No hay PRIMARY activo, buscar un BACKUP vivo
        for sid, info in api_servers.items():
            last_hb = datetime.fromisoformat(info["last_heartbeat"])
            elapsed = (now - last_hb).total_seconds()
            
            if elapsed < API_SERVER_TIMEOUT:
                # Promover este BACKUP a PRIMARY con nuevo epoch
                global primary_epoch
                primary_epoch += 1
                lease_expires = now.timestamp() + LEASE_DURATION
                api_servers[sid]["role"] = "PRIMARY"
                api_servers[sid]["primary_epoch"] = primary_epoch
                api_servers[sid]["lease_expires_at"] = datetime.fromtimestamp(lease_expires).isoformat()
                logger.warning(f"[{server_id}] No hay PRIMARY. Promoviendo {sid} a PRIMARY en resolución (epoch={primary_epoch})")
                
                return APIServerResolveResponse(
                    server_id=sid,
                    ip=info["ip"],
                    port=info["port"],
                    role="PRIMARY",
                    url=f"http://{info['ip']}:{info['port']}",
                    primary_epoch=primary_epoch,
                    lease_expires_at=api_servers[sid]["lease_expires_at"]
                )
        
        logger.error(f"[{server_id}] No hay servidores API disponibles")
        raise HTTPException(status_code=503, detail="No hay servidores API disponibles")


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
async def register_processor(request: ProcessorRegisterRequest, background_tasks: BackgroundTasks):
    """
    Registra un Processor Node en el DNS.
    Los processors son stateless y todos tienen el mismo peso.
    El registro se propaga automáticamente a todos los otros DNS.
    """
    async with processor_servers_lock:
        now = datetime.now().isoformat()
        
        processor_info = {
            "ip": request.ip,
            "port": request.port,
            "last_heartbeat": now,
            "registered_at": now,
            "healthy": True
        }
        
        processor_servers[request.processor_id] = processor_info
        
        logger.info(f"[{server_id}] Processor {request.processor_id} registrado ({request.ip}:{request.port})")
        
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
        
        # Round-robin simple: usar el primero de la lista ordenada
        # (en producción podrías implementar un índice rotativo)
        active_processors.sort(key=lambda x: x[0])
        pid, info = active_processors[0]
        
        logger.info(f"[{server_id}] Resolución de processor -> {pid} ({info['ip']}:{info['port']})")
        
        return ProcessorResolveResponse(
            processor_id=pid,
            ip=info["ip"],
            port=info["port"],
            url=f"http://{info['ip']}:{info['port']}"
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


@app.post("/notify-new-primary")
async def receive_new_primary_notification(data: dict):
    """Recibe notificación de un nuevo primario"""
    global server_role, current_primary_url, primary_since
    
    new_primary_id = data.get("new_primary_id")
    new_primary_url = data.get("new_primary_url")
    new_primary_since = data.get("primary_since")
    
    logger.info(f"[{server_id}] Notificación: nuevo primario es {new_primary_id}")
    
    if server_role == "primary" and new_primary_id != server_id:
        # Resolver conflicto: el que fue primario primero gana
        if new_primary_since and primary_since:
            if new_primary_since < primary_since:
                logger.warning(f"[{server_id}] {new_primary_id} fue primario antes. Cediendo rol...")
                server_role = "backup"
                current_primary_url = new_primary_url
                primary_since = None
        else:
            # Sin timestamp, ceder por defecto
            server_role = "backup"
            current_primary_url = new_primary_url
            primary_since = None
    
    if new_primary_id in cluster_state:
        cluster_state[new_primary_id]["role"] = "primary"
        cluster_state[new_primary_id]["primary_since"] = new_primary_since
    
    current_primary_url = new_primary_url
    
    return {"status": "acknowledged", "server_id": server_id}


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
    Determina el rol de este servidor basándose en el estado del clúster.
    """
    global server_role, current_primary_url, primary_since
    
    # Buscar si hay un primario activo
    active_primary = None
    for sid, info in cluster_state.items():
        if sid == server_id:
            continue
        if info.get("role") == "primary" and info.get("healthy"):
            active_primary = (sid, info)
            break
    
    if active_primary:
        # Ya hay un primario, soy backup
        if server_role != "backup":
            logger.info(f"[{server_id}] Primario activo: {active_primary[0]}. Configurándome como BACKUP")
            server_role = "backup"
            primary_since = None
            current_primary_url = active_primary[1].get("url")
            
            # Sincronizar inmediatamente
            await sync_from_primary()
    else:
        # No hay primario activo
        if server_role == "primary":
            # Ya soy primario, mantener
            pass
        else:
            # Verificar si debo promoverme
            should_promote = True
            
            # Verificar si hay otro servidor que debería ser primario antes
            for sid, info in cluster_state.items():
                if sid == server_id:
                    continue
                if not info.get("healthy"):
                    continue
                    
                # Si hay otro servidor que fue primario antes, esperar
                other_primary_since = info.get("primary_since")
                if other_primary_since and primary_since:
                    if other_primary_since < primary_since:
                        should_promote = False
                        break
            
            if should_promote:
                logger.warning(f"[{server_id}] No hay primario activo. PROMOVIENDO A PRIMARY...")
                server_role = "primary"
                primary_since = time.time()
                current_primary_url = None
                
                # Actualizar mi entrada en cluster_state
                if server_id in cluster_state:
                    cluster_state[server_id]["role"] = "primary"
                    cluster_state[server_id]["primary_since"] = primary_since
                
                # Notificar a otros
                await notify_promotion()
                
                logger.info(f"[{server_id}] *** PROMOCIÓN COMPLETADA: Ahora soy el PRIMARY ***")


async def notify_promotion():
    """Notifica a otros servidores que me he promovido a primario"""
    for sid, info in cluster_state.items():
        if sid == server_id or not info.get("healthy"):
            continue
            
        url = info.get("url")
        if not url:
            continue
            
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                await client.post(f"{url}/notify-new-primary", json={
                    "new_primary_id": server_id,
                    "new_primary_url": f"http://{my_ip}:{dns_port}",
                    "primary_since": primary_since,
                    "timestamp": datetime.now().isoformat()
                })
                logger.info(f"[{server_id}] Notificación de promoción enviada a {sid}")
        except Exception as e:
            logger.error(f"[{server_id}] Error notificando a {sid}: {e}")


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
    
    logger.info(f"[{server_id}] DNS Service HA iniciado. Rol: {server_role}")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=dns_port)
