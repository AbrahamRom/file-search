# Informe Detallado: Sistema de Sharding - File Search Cluster

**Fecha**: 5 de enero de 2026  
**Componente**: Distributed File Search System  
**Versión**: 3.0.0 con Sharding y Epoch Fencing

---

## Tabla de Contenidos

1. [Introducción](#introducción)
2. [Arquitectura General](#arquitectura-general)
3. [Mecanismo de Sharding](#mecanismo-de-sharding)
4. [Asignación de Archivos a Shards](#asignación-de-archivos-a-shards)
5. [Placement y Replicación](#placement-y-replicación)
6. [Caching de Membership](#caching-de-membership)
7. [Sincronización de Shards](#sincronización-de-shards)
8. [Fencing y Consistencia](#fencing-y-consistencia)
9. [Flujo de Operaciones](#flujo-de-operaciones)
10. [Configuración](#configuración)

---

## Introducción

El sistema de sharding implementado en File Search distribuye datos de forma **determinística** y **agnóstica** entre múltiples nodos de almacenamiento. El objetivo es:

- **Escalabilidad horizontal**: Agregar más nodos permite distribuir más datos
- **Independencia de datos**: Cada shard es un conjunto independiente de archivos
- **Replicación dirigida**: Cada shard se replica solo a los nodos que lo necesitan
- **Failover automático**: Cuando el primario cae, los backups se promocionan sin perder datos
- **Consistencia fuerte**: Epoch fencing previene writes concurrentes a múltiples primarios

### Características Principales

| Característica | Implementación |
|---|---|
| **Estrategia de sharding** | Hash-based (MD5) o Alfabética |
| **Número de shards configurable** | `NUM_SHARDS` (default: 3) |
| **Replicación por shard** | `REPLICAS_REQUIRED` (default: 3) |
| **Consensus** | DNS primario solo (solo-primario) |
| **Fencing de writes** | Epoch-based con lease validation |
| **Cache de membership** | TTL de 5 segundos con epoch invalidation |
| **Sincronización** | Full-sync + shard-scoped replication |

---

## Arquitectura General

```
┌─────────────────────────────────────────────────────────────┐
│                     CLIENTE (Processor)                      │
│  - Calcula shard_id = hash(filename) % NUM_SHARDS            │
│  - Pregunta a DNS: "¿Dónde está el shard X?"                │
│  - Envía archivo al nodo primario del shard                 │
└─────────────────────────────────────────────────────────────┘
                            ↓
┌─────────────────────────────────────────────────────────────┐
│                   DNS SERVICE HA (3 instancias)              │
│  - Mantiene shard_map global: {shard_id → placement}        │
│  - Solo el primario escribe en shard_map                    │
│  - Backups replican el estado cada 5 segundos              │
│  - Cada shard tiene: primary, replicas[], epoch             │
└─────────────────────────────────────────────────────────────┘
                    ↓         ↓         ↓
        ┌──────────┴──────────┴──────────┐
        ↓           ↓           ↓
    ┌────────┐ ┌────────┐ ┌────────┐
    │ Shard  │ │ Shard  │ │ Shard  │
    │  0     │ │  1     │ │  2     │
    │        │ │        │ │        │
    │ P  B1 B2│ │P  B1 B2│ │P  B1 B2│
    └────────┘ └────────┘ └────────┘
     Storage₁  Storage₂  Storage₃
```

**Clave**: 
- `P` = Primary (acepta writes)
- `B1, B2` = Backups (solo lecturas, se sincronizan)
- Cada almacenamiento contiene réplicas de múltiples shards

---

## Mecanismo de Sharding

### 1. Definición de Shard ID

El sistema soporta **dos estrategias** para asignar archivos a shards:

#### Estrategia Hash (por defecto)

```python
def compute_shard_key(filename: str, strategy: str = "hash") -> str:
    if strategy == "hash":
        hash_val = int(hashlib.md5(filename.encode()).hexdigest(), 16)
        num_shards = int(os.getenv("NUM_SHARDS", 2))
        return f"shard_{hash_val % num_shards}"
```

**Cálculo**:
1. Tomar nombre del archivo
2. Calcular MD5 del nombre en UTF-8
3. Convertir hexadecimal a entero
4. Aplicar módulo `% NUM_SHARDS`
5. Retornar `shard_{resultado}`

**Ejemplo**:
```
Archivo: "documento.pdf"
MD5: 91a85a8a6e9d9a5e...
Entero: 12345678901234567
NUM_SHARDS: 3
Resultado: 12345678901234567 % 3 = 1
Shard ID: "shard_1"
```

**Ventajas**:
- Determinístico: mismo archivo siempre va al mismo shard
- Uniforme: distribución balanceada (cada shard ~33% de archivos)
- Agnóstico: sin conocer el contenido del archivo

#### Estrategia Alfabética

```python
if strategy == "alpha":
    first_char = filename[0].upper() if filename else "A"
    if first_char < "N":
        return "shard_a_m"    # A-M
    else:
        return "shard_n_z"    # N-Z
```

Divide archivos por primera letra (A-M vs N-Z).

### 2. Cálculo en el Processor Node

Cuando un cliente sube un archivo:

```python
# Processor: endpoints.py línea ~600
num_shards = int(os.getenv("NUM_SHARDS", 1))
shard_strategy = os.getenv("SHARD_STRATEGY", "hash")

if not shard_id and num_shards > 1:
    path_for_hash = f"{folder}/{file.filename}" if folder else file.filename
    shard_id = compute_shard_key(path_for_hash, strategy=shard_strategy)
```

**Flujo**:
1. Cliente no especifica `shard_id` en request
2. Processor calcula: `shard_id = hash(filename) % NUM_SHARDS`
3. Processor pregunta a DNS: `GET /shard/resolve/shard_1`
4. DNS responde con `{primary_url, replicas, epoch}`
5. Processor envía archivo al primario

---

## Asignación de Archivos a Shards

### Modelo de Almacenamiento por Nodo

Cada Storage Node contiene **archivos de múltiples shards**:

```
Storage Node 1:
├── files/
│   ├── shard_0/
│   │   ├── archivo1.txt (hash % 3 = 0)
│   │   ├── archivo4.txt (hash % 3 = 0)
│   │   └── ...
│   ├── shard_1/
│   │   ├── archivo2.txt (hash % 3 = 1)
│   │   └── ...
│   └── shard_2/
│       ├── archivo3.txt (hash % 3 = 2)
│       └── ...
└── database.db
    └── files table (contiene file_id, shard_id, etc.)
```

### Validación de Membership

Cuando Storage recibe un archivo en shard X, verifica con DNS:

**1. Cache ligero (fase 3)**:
```python
# Formato: {shard_id: {"data": {...}, "epoch": int, "cached_at": float}}
_shard_membership_cache = {}
SHARD_CACHE_TTL = 5.0  # 5 segundos
```

**2. Verificación con DNS** (si cache no está válido):
```python
# Línea 130 de endpoints.py
url = f"http://{dns_alias}:{dns_port}/shard/resolve/{shard_id}"
resp = httpx.get(url, timeout=3.0)
data = resp.json()  # {primary, replicas, epoch, ...}
```

**3. Validación**: 
```python
if STORAGE_ID not in replicas:
    raise HTTPException(403, "Storage node is not a replica of this shard")
```

---

## Placement y Replicación

### 1. Mapa de Placement (shard_map)

En DNS primario, se mantiene `shard_map` global:

```python
shard_map: Dict[str, dict] = {
    "shard_0": {
        "primary": "storage_1",
        "replicas": ["storage_1", "storage_2", "storage_3"],
        "epoch": 42,
        "primary_url": "http://storage_1:8000",
        "pending_sync": False,
        "sync_status": "synced"
    },
    "shard_1": {
        "primary": "storage_2",
        "replicas": ["storage_2", "storage_3", "storage_1"],
        "epoch": 42,
        ...
    },
    "shard_2": {
        "primary": "storage_3",
        "replicas": ["storage_3", "storage_1", "storage_2"],
        "epoch": 42,
        ...
    }
}
```

### 2. Algoritmo de Placement Adaptativo

En `dns_service/main.py` líneas 2248-2450:

**Objetivo**: Asignar cada shard a `REPLICAS_REQUIRED` nodos de forma balanceada.

```
ensure_shard_placement(shard_id="shard_0", replicas_required=3)
```

**Pasos**:

1. **Obtener nodos vivos**:
   ```python
   alive_nodes = await get_alive_storage_nodes()
   # Nodos que han hecho heartbeat en los últimos 25 segundos
   ```

2. **Ajuste adaptativo** (si hay pocos nodos):
   ```python
   actual_replicas_required = min(replicas_required, len(alive_nodes))
   # Si solo hay 2 nodos vivos y requierimos 3, ajustamos a 2
   ```

3. **Si ya existe placement**:
   - Mantener las réplicas vivas existentes
   - Si primary cayó, promover un backup
   - Marcar como `pending_sync=True` para sincronización en background
   - Incrementar `epoch` cuando hay cambios

4. **Si no existe placement**:
   - Seleccionar el nodo preferido (si está vivo)
   - Asignar replicación a otros nodos balanceando réplicas existentes
   - Crear placement con `epoch=1, pending_sync=True`

### 3. Replicación Inicial

Cuando se crea un nuevo shard:

```python
# DNS primario llama a full_sync en backups
async def _request_full_sync(target_id: str, source_id: str) -> bool:
    resp = await client.post(
        f"{target_url}/internal/full-sync",
        params={"primary_url": source_url}
    )
```

**Flow**:
1. Storage target descarga snapshot de DB del source
2. Storage target descarga todos los archivos del source
3. Marca placement como `pending_sync=False, sync_status=synced`
4. DNS incrementa epoch para notificar cambio

---

## Caching de Membership

### Problema que Resuelve

Sin cache:
- Cada READ a un shard requiere validar membership con DNS
- DNS recibe miles de requests/segundo
- Cuellos de botella en DNS

### Solución: Cache TTL de 5 segundos

```python
# Línea 69-180 de endpoints.py
def _ensure_shard_membership(shard_id, require_primary=False):
    cached = _shard_membership_cache.get(shard_id)
    if cached and (current_time - cached["cached_at"]) < SHARD_CACHE_TTL:
        # Usar datos cacheados (máximo 5 segundos de antigüedad)
        return cached["data"]
    
    # Cache miss: consultar DNS
    resp = httpx.get(f"http://dns:5353/shard/resolve/{shard_id}")
    data = resp.json()
    
    # Invalidar cache si epoch cambió
    if cached and cached["epoch"] != data["epoch"]:
        _shard_membership_cache.pop(shard_id)
    
    # Actualizar cache
    _shard_membership_cache[shard_id] = {
        "data": data,
        "epoch": data["epoch"],
        "cached_at": current_time
    }
```

### Invalidación Basada en Epoch

Si DNS incrementa el epoch (por failover o rebalanceo):
1. Storage recibe respuesta con `epoch=43` (antes era 42)
2. Cache detecta cambio: `42 != 43`
3. Cache se invalida automáticamente
4. Próxima validación consultará DNS fresco

### Beneficio Cuantificable

```
Sin cache:
- 1000 archivos/segundo × 3 consultas DNS/archivo = 3000 req/s a DNS

Con cache (5s TTL):
- 1000 archivos/segundo × 0.2 consultas DNS/archivo ≈ 200 req/s a DNS
- Reducción: 93% menos requests a DNS
```

---

## Sincronización de Shards

### 1. Sincronización Inicial (full_sync)

Cuando se asigna un backup a un shard:

**En el Backup Storage**:
```python
async def full_sync(self) -> bool:
    """Descarga BD snapshot + archivos desde primario"""
    # 1. Descargar snapshot de BD
    resp = await client.get(f"{primary_url}/internal/backup/db")
    with open(DB_PATH, "wb") as f:
        f.write(resp.content)
    
    # 2. Descargar archivos (llamar a sync_files())
    result = await self.sync_files()
```

**En el Primario Storage**:
```python
@app.get("/internal/backup/db")
async def get_db_backup():
    """Endpoint para backups descarguen DB completa"""
    return FileResponse(DB_PATH)
```

### 2. Sincronización Dirigida por Shard (replicate_shard)

Más eficiente que full_sync cuando cambia solo un shard:

**En el Backup Storage**:
```python
# Línea 404-475 de sync_service.py
async def replicate_shard(self, shard_id: str, source_node_url: str):
    """Sincroniza solo un shard específico"""
    
    # 1. Obtener lista de archivos del shard
    resp = await client.get(
        f"{source_node_url}/internal/files",
        params={"shard_id": shard_id}
    )
    remote_files = resp.json()["files"]
    
    # 2. Descargar solo archivos nuevos o modificados
    for file in remote_files:
        if not file_exists_locally(file):
            download_file_from_source(file)
        elif file["last_modified"] > local_file["last_modified"]:
            download_file_from_source(file)
```

**Triggered por DNS**:
```python
# Línea 2151-2207 de main.py
async def _trigger_shard_replication(shard_id, source_node_id, target_node_ids):
    """DNS envía POST a cada target: 'sincroniza este shard desde source'"""
    
    for target_id in target_node_ids:
        resp = await client.post(
            f"{target_url}/internal/replicate-shard",
            params={
                "shard_id": shard_id,
                "source_node_url": source_url
            }
        )
```

### 3. Last-Modified-Wins (LWW)

Para resolver conflictos durante replicación:

```python
# En replicate_shard()
if remote_file["last_modified"] > local_file["last_modified"]:
    # Descargar versión más reciente del remoto
    download_file()
else if local_file["last_modified"] > remote_file["last_modified"]:
    # Empujar versión local al remoto
    push_file_to_source()
```

**Ventaja**: Converge automáticamente sin necesidad de versioning complejo.

---

## Fencing y Consistencia

### Problema: Split-Brain

Si DNS tiene un outage total:
- Storage₁ cree que es PRIMARY de shard_0
- Storage₂ también cree que es PRIMARY de shard_0
- Ambas escriben datos conflictivos

### Solución: Epoch Fencing (3 capas)

#### Capa 1: Solo-Primario en DNS

```python
# main.py línea 2248
async def ensure_shard_placement(...):
    async with shard_lock:
        if server_role != "primary":
            # Backups DNS nunca escriben en shard_map
            if existing:
                return existing
            else:
                raise HTTPException(503, "Consulte DNS primario")
```

**Garantía**: Solo el DNS primario puede cambiar shard_map.

#### Capa 2: Validación de Lease

```python
# node_manager.py línea 95
def has_valid_lease(self) -> bool:
    if not self.is_primary or not self._lease_expires_at:
        return False
    return time.time() < self._lease_expires_at
```

Cuando Storage recibe promotion a PRIMARY:
- DNS asigna `lease_expires_at = now + LEASE_DURATION (30 segundos)`
- Storage rechaza writes si lease expiró
- Heartbeat renovado periódicamente desde DNS

**Flow**:
```
DNS primario: "Storage₁, eres PRIMARY de shard_0"
              lease_expires_at = 10:00:30

Storage₁ (10:00:15): Lease válida (30s - 15s = 15s restante) ✓ Acepta writes
Storage₁ (10:00:35): Lease expirada (30s < 35s) ✗ Rechaza writes
```

#### Capa 3: Epoch Validation

```python
# endpoints.py línea 572-588
shard_info = _ensure_shard_membership(shard_id, require_primary=True)

if shard_info:
    shard_epoch = shard_info.get("epoch", 0)
    node_mgr = get_node_manager()
    if node_mgr.primary_epoch and node_mgr.primary_epoch < shard_epoch:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "stale_epoch",
                "storage_epoch": node_mgr.primary_epoch,
                "shard_epoch": shard_epoch
            }
        )
```

**Escenario**:
1. DNS primario promueve Storage₁ a PRIMARY con epoch=42
2. Notifica a Storage₁ vía heartbeat: `primary_epoch=42`
3. DNS primario falla, backup toma control
4. Backup observa que DNS tuvo outage > 30 segundos
5. Promueve Storage₂ a PRIMARY con epoch=43
6. Storage₁ intenta write:
   - Valida: `my_epoch=42`, `shard_epoch=43`
   - Detecta: `42 < 43` (estoy atrasado)
   - Rechaza write con HTTP 409

**Resultado**: Writes no pueden ejecutarse en nodo "viejo".

---

## Flujo de Operaciones

### Flujo 1: Upload de Archivo

```
Cliente (Processor₁)
│
├─ 1: POST /upload {"file": "documento.pdf", "folder": "docs"}
│
└─ 2: Processor calcula: shard_id = hash("docs/documento.pdf") % 3 = "shard_1"
      (línea ~560 endpoints.py)
      
      3: Processor pregunta a DNS: GET /shard/resolve/shard_1
         Timeout 3 segundos
         
      4: DNS responde:
         {
           "primary": "storage_2",
           "primary_url": "http://storage_2:8000",
           "replicas": ["storage_2", "storage_3", "storage_1"],
           "epoch": 42
         }
      
      5: Processor envía: POST http://storage_2:8000/upload
         {
           "file": "documento.pdf",
           "shard_id": "shard_1",
           "folder": "docs"
         }

Storage₂ (PRIMARY de shard_1)
│
├─ 6: Recibe upload, valida:
│     - ¿Es mi shard? Verificar DNS con cache (línea 82-181)
│       Cache hit → 5ms
│       Cache miss → consultar DNS (100ms)
│     
│     - ¿Soy PRIMARY? (línea 589-595)
│       Verificar node_manager.is_primary
│     
│     - ¿Lease válido? (línea 596-602)
│       time.now() < lease_expires_at
│     
│     - ¿Epoch válido? (línea 572-588)
│       my_epoch (42) >= shard_epoch (42)
│
├─ 7: Calcula hash del contenido (SHA256)
│
├─ 8: Guarda archivo en /app/files/docs/documento.pdf
│
├─ 9: Inserta en BD:
│     files {
│       file_id: SHA1(relative_path),
│       name: "documento.pdf",
│       shard_id: "shard_1",
│       write_epoch: 42,
│       origin_node: "storage_2"
│     }
│
└─10: Retorna HTTP 200 + metadata

Backups (Storage₃, Storage₁)
│
├─11: Reciben sincronización en background
│     - Si pending_sync: Esperan trigger de DNS
│     - DNS llama: POST /internal/replicate-shard?shard_id=shard_1
│     
│     - Storage₃ y Storage₁ descargan:
│       GET http://storage_2:8000/internal/files?shard_id=shard_1
│       GET http://storage_2:8000/internal/file/{relative_path}
│
└─12: Almacenan copia local, actualizan BD
```

### Flujo 2: Failover de Primario

```
Escenario: Storage₂ (PRIMARY de shard_1) falla

1. Storage₂ detiene heartbeat a DNS

2. DNS detecta timeout (> 25 segundos sin heartbeat)
   - Elimina storage_2 de api_servers (línea 2695)
   - Marca shards afectados para revisión

3. Próxima resolución: GET /shard/resolve/shard_1
   
4. DNS corre ensure_shard_placement(shard_1):
   - Obtiene: alive_nodes = [storage_1, storage_3]
   - Placement actual: primary=storage_2 (MUERTO)
   - Promueve: new_primary=storage_3 (primer backup vivo)
   - Incrementa: epoch = 42 → 43
   - Marca: pending_sync=True
   - Actualiza shard_map[shard_1] = {..., primary: storage_3, epoch: 43}
   - Persiste epoch en DB (SHARD_EPOCH_META_KEY)
   - Llama: _trigger_shard_replication(shard_1, storage_3, [storage_1])

5. Processor intenta resolver shard_1 nuevamente:
   GET /shard/resolve/shard_1
   
6. DNS responde con datos nuevos:
   {
     "primary": "storage_3",
     "primary_url": "http://storage_3:8000",
     "replicas": ["storage_3", "storage_1", "storage_2_DEAD"],
     "epoch": 43,
     "pending_sync": True
   }

7. Processor envía next write a storage_3 (nuevo primary)

8. Storage₁ recibe trigger:
   POST /internal/replicate-shard?shard_id=shard_1&source=http://storage_3:8000
   - Descarga archivos nuevos desde storage_3
   - Actualiza BD local

9. Timeout (5-10 minutos), DNS intenta resync storage_2
   - Si storage_2 reaparece:
     * DNS lo detecta vía heartbeat
     * Lo agrega a alive_nodes
     * Asigna como backup nuevamente
   - Si storage_2 sigue caído:
     * DNS mantiene replicas = [storage_3, storage_1]
     * Actualiza epoch = 44
```

### Flujo 3: Lectura con Cache Fallback

```
Escenario: DNS servicio completamente caído

Cliente busca archivo en shard_0

Storage₁ recibe: GET /files?shard_id=shard_0

1. Intenta validar membership normalmente:
   - Consulta cache → válido (< 5 segundos)
   - Retorna datos cacheados ✓

2. Si cache expirado:
   - Intenta conectar a DNS (timeout 3s)
   - DNS no responde → exception
   - _ensure_shard_membership_or_redirect() entra en modo resiliente
   
3. Modo resiliente (línea 226-245):
   ```python
   if request.method == "GET":  # Lectura
       # Intentar usar cache expirado
       if cached:
           logger.info("[RESILIENT READ] Usando cache expirado...")
           return cached["data"]  # Datos de hace 5-10 segundos
   ```

4. Si la lectura es no-idempotente (POST/PUT):
   ```python
   if request.method in ["POST", "PUT", "DELETE"]:  # Escritura
       # Rechazar: fencing requiere consensus
       raise HTTPException(503, "Cannot write without DNS")
   ```

Resultado:
- ✓ Lecturas: Disponibles (con datos potencialmente viejos)
- ✗ Escrituras: Bloqueadas (mantiene consistencia)
```

---

## Configuración

### Variables de Entorno

| Variable | Ubicación | Valor Default | Descripción |
|----------|-----------|---------------|-------------|
| `NUM_SHARDS` | Storage, Processor, DNS | 3 | Número total de shards |
| `SHARD_STRATEGY` | Storage, Processor | hash | "hash" o "alpha" |
| `REPLICAS_REQUIRED` | Storage, DNS | 3 | Factor de replicación |
| `SHARD_CACHE_TTL` | Storage | 5.0 | TTL cache membership (segundos) |
| `LEASE_DURATION` | DNS | 30 | Duración de lease PRIMARY (segundos) |
| `HOST_ID` | Storage | hostname | Identificador de host físico |
| `STORAGE_ID` | Storage | storage_X | Identificador del nodo storage |
| `DNS_ALIAS` | Todos | dns | Alias Docker para DNS |
| `DNS_SERVICE_PORT` | Todos | 5353 | Puerto DNS |

### Comandos de Deployment

```bash
# Host 1
docker run -d \
  --name storage_1 \
  -e NUM_SHARDS=3 \
  -e SHARD_STRATEGY=hash \
  -e REPLICAS_REQUIRED=3 \
  -e HOST_ID=host_1 \
  -e SHARD_CACHE_TTL=5 \
  -e LEASE_DURATION=30 \
  file-search-storage:latest

# Host 2
docker run -d \
  --name storage_2 \
  -e NUM_SHARDS=3 \
  -e SHARD_STRATEGY=hash \
  -e REPLICAS_REQUIRED=3 \
  -e HOST_ID=host_2 \
  -e SHARD_CACHE_TTL=5 \
  -e LEASE_DURATION=30 \
  file-search-storage:latest
```

---

## Ejemplos Prácticos

### Ejemplo 1: 3 Archivos, 3 Shards, 3 Replicas

```
Archivos subidos:
├── "report.pdf" → hash % 3 = 0 → shard_0
├── "image.jpg" → hash % 3 = 1 → shard_1
└── "data.csv" → hash % 3 = 2 → shard_2

Placement (NUM_SHARDS=3, REPLICAS_REQUIRED=3):
┌─────────────────────────────────────────┐
│ shard_0: primary=Storage₁               │
│          replicas=[Storage₁, Storage₂, Storage₃]│
├─────────────────────────────────────────┤
│ shard_1: primary=Storage₂               │
│          replicas=[Storage₂, Storage₃, Storage₁]│
├─────────────────────────────────────────┤
│ shard_2: primary=Storage₃               │
│          replicas=[Storage₃, Storage₁, Storage₂]│
└─────────────────────────────────────────┘

Storage₁ almacena:
  report.pdf (shard_0 - primary)
  image.jpg (shard_1 - backup)
  data.csv (shard_2 - backup)

Storage₂ almacena:
  report.pdf (shard_0 - backup)
  image.jpg (shard_1 - primary)
  data.csv (shard_2 - backup)

Storage₃ almacena:
  report.pdf (shard_0 - backup)
  image.jpg (shard_1 - backup)
  data.csv (shard_2 - primary)
```

### Ejemplo 2: Escalabilidad

```
Escenario: Crecimiento de 3 a 5 nodos storage

Paso 1: Agregar Storage₄ y Storage₅
        Actualizar NUM_SHARDS = 5 (ó mantener = 3)

Si NUM_SHARDS = 5:
  Redistribución de datos (hash no es consistente):
  - "report.pdf": shard_0 → shard_? (recalcular hash)
  - Problema: Requiere re-hash de todos los archivos
  
Si NUM_SHARDS = 3 (mantener):
  - Agregar Storage₄ y Storage₅ como replicas adicionales
  - Cada shard ahora tiene 5 replicas en lugar de 3
  - REPLICAS_REQUIRED puede bajar a 2 (más tolerancia a fallos)
  - Sin re-hashing de datos
```

---

## Monitoreo y Debugging

### Endpoint para Inspeccionar Estado

```bash
# Estado del DNS primario
curl http://localhost:5353/status

# Resolver un shard específico
curl http://localhost:5353/shard/resolve/shard_0

# Listar todos los shards
curl http://localhost:5353/shards

# Estado de un nodo storage
curl http://localhost:9000/status

# Archivos en un shard específico
curl "http://localhost:9000/files?shard_id=shard_0"
```

### Logs Importantes

**DNS Service**:
```
[dns_service] Shard shard_0: ajustando replicas_required de 3 a 2
[dns_service] Shard shard_1: promoviendo storage_3 a PRIMARY (epoch 42 → 43)
[dns_service] STALE EPOCH: Shard shard_0 en memoria (epoch=42) vs DB (epoch=43)
```

**Storage Service**:
```
[storage] Invalidando cache de shard shard_0: epoch 42 → 43
[storage] Upload rejected: write_epoch 41 < shard_epoch 42
[storage] [RESILIENT READ] Usando cache expirado para shard shard_0
[storage] Replicating shard shard_1 from http://storage_1:8000
```

---

## Conclusión

El sistema de sharding implementado proporciona:

1. **Escalabilidad**: Agregar storage nodes mejora capacity lineal
2. **Disponibilidad**: Failover automático sin intervention manual
3. **Consistencia**: Epoch fencing previene split-brain
4. **Eficiencia**: Cache TTL reduce carga en DNS en 93%
5. **Simplicity**: Algoritmo hash determinístico, sin redistribución

La arquitectura es production-ready con 3 nodos storage y 3 DNS HA replicados.
