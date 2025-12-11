# Arquitectura Separada: Storage + Processor

## Visión General

Este documento describe la nueva arquitectura separada del sistema File Search, que divide el servidor monolítico original en dos capas:

```
┌─────────────────────────────────────────────────────────────────────┐
│                           CLIENTE                                    │
│                        (Streamlit Web)                              │
└─────────────────────────────────┬───────────────────────────────────┘
                                  │
                                  ▼
┌─────────────────────────────────────────────────────────────────────┐
│                      PROCESSOR NODES                                 │
│                    (Stateless, N instancias)                        │
│  ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐     │
│  │  processor_1    │  │  processor_2    │  │  processor_N    │     │
│  │  Circuit Breaker│  │  Circuit Breaker│  │  Circuit Breaker│     │
│  │  Scatter-Gather │  │  Scatter-Gather │  │  Scatter-Gather │     │
│  └────────┬────────┘  └────────┬────────┘  └────────┬────────┘     │
└───────────┼────────────────────┼────────────────────┼───────────────┘
            │                    │                    │
            ▼                    ▼                    ▼
┌─────────────────────────────────────────────────────────────────────┐
│                       STORAGE NODES                                  │
│                   (PRIMARY + BACKUP, con sync)                      │
│  ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐     │
│  │  storage_1      │  │  storage_2      │  │  storage_3      │     │
│  │  [PRIMARY]      │  │  [BACKUP]       │  │  [BACKUP]       │     │
│  │  SQLite + Files │  │  SQLite + Files │  │  SQLite + Files │     │
│  └─────────────────┘  └─────────────────┘  └─────────────────┘     │
│           │                    ▲                    ▲               │
│           └────────────────────┴────────────────────┘               │
│                          Sincronización                             │
└─────────────────────────────────────────────────────────────────────┘
```

## Componentes

### 1. Storage Nodes (`app/storage/`)

**Responsabilidades:**
- Base de datos SQLite con metadata de archivos
- Almacenamiento físico de archivos
- Sincronización PRIMARY-BACKUP automática
- API REST interna para operaciones CRUD

**Características:**
- Replicación completa (todos los nodos tienen todos los datos)
- Failover automático si PRIMARY falla
- Sincronización cada 10 segundos (configurable)

**Endpoints:**
| Endpoint | Método | Descripción |
|----------|--------|-------------|
| `/health` | GET | Health check |
| `/status` | GET | Estado detallado del nodo |
| `/files` | GET | Listar archivos |
| `/files/{id}` | GET | Obtener metadata |
| `/files/{id}` | DELETE | Eliminar archivo |
| `/files/{id}/download` | GET | Descargar archivo |
| `/search` | GET | Buscar por nombre |
| `/upload` | POST | Subir archivo |
| `/internal/db_snapshot` | GET | Snapshot de BD (sync) |
| `/internal/files` | GET | Lista archivos físicos (sync) |
| `/internal/file/{path}` | GET | Descargar archivo (sync) |

### 2. Processor Nodes (`app/processor/`)

**Responsabilidades:**
- Recibir peticiones del cliente
- Coordinar operaciones con Storage Nodes
- Circuit breaker para tolerancia a fallos
- Failover automático a Storage Nodes de respaldo

**Características:**
- **Stateless**: Puede escalarse horizontalmente sin coordinación
- **Circuit Breaker**: Previene cascadas de fallos
- **Scatter-Gather**: Preparado para queries distribuidas (sharding futuro)

**Endpoints:**
Mismos que el servidor original, compatible con el cliente existente:
| Endpoint | Método | Descripción |
|----------|--------|-------------|
| `/health` | GET | Health check |
| `/status` | GET | Estado del procesador |
| `/files` | GET | Listar archivos |
| `/files/{id}/download` | GET | Descargar archivo |
| `/search` | GET | Buscar archivos |
| `/upload` | POST | Subir archivo |
| `/node/status` | GET | Compatibilidad con cliente |

### 3. DNS Service (sin cambios)

El servicio DNS existente sigue funcionando. Los Storage Nodes se registran usando la misma API `/server/register`.

## Circuit Breaker

El Processor implementa el patrón Circuit Breaker para cada Storage Node:

```
Estados:
  CLOSED (normal) ──[5 fallos]──► OPEN (rechaza)
       ▲                              │
       │                              │ [30s timeout]
       │                              ▼
       └──────[3 éxitos]───── HALF_OPEN (prueba)
```

**Configuración:**
- `failure_threshold`: 5 fallos para abrir
- `recovery_timeout`: 30 segundos antes de probar
- `half_open_max_calls`: 3 éxitos para cerrar

## Archivos de Configuración

### docker-compose.separated.yml
Configuración para desarrollo local con la nueva arquitectura.

### stack.separated.yml
Configuración para producción con Docker Swarm.

### Puertos

| Servicio | Puerto Local | Puerto Interno |
|----------|--------------|----------------|
| DNS 1 | 5353 | 5353 |
| DNS 2 | 5354 | 5353 |
| DNS 3 | 5355 | 5353 |
| Storage 1 | 9000 | 8000 |
| Storage 2 | 9001 | 8000 |
| Storage 3 | 9002 | 8000 |
| Processor 1 | 8000 | 8000 |
| Processor 2 | 8001 | 8000 |
| Client | 8501 | 8501 |

## Uso

### Desarrollo Local

```bash
# Construir imágenes
docker build -t file-search-storage:latest -f Dockerfile.storage .
docker build -t file-search-processor:latest -f Dockerfile.processor .

# Levantar servicios
docker compose -f docker-compose.separated.yml up -d

# Ver logs
docker compose -f docker-compose.separated.yml logs -f
```

### Producción (Swarm)

```bash
# Construir imágenes
docker build -t file-search-storage:latest -f Dockerfile.storage .
docker build -t file-search-processor:latest -f Dockerfile.processor .

# Desplegar stack
docker stack deploy -c stack.separated.yml file-search
```

## Preparado para el Futuro

### 1. Sharding (Particionamiento Horizontal)

El esquema de BD incluye `shard_id`:
```sql
CREATE TABLE files (
    ...
    shard_id TEXT DEFAULT NULL  -- Para futuro sharding
);
```

El Processor incluye `scatter_gather_search()` para queries distribuidas.

### 2. Full-Text Search (Índice Invertido)

El esquema incluye tablas preparadas:
```sql
CREATE TABLE keywords (
    keyword_id INTEGER PRIMARY KEY,
    keyword TEXT NOT NULL UNIQUE,
    doc_count INTEGER DEFAULT 1
);

CREATE TABLE file_keywords (
    keyword TEXT NOT NULL,
    file_id TEXT NOT NULL,
    frequency INTEGER DEFAULT 1,
    PRIMARY KEY (keyword, file_id)
);
```

### 3. Estrategias de Sharding

```python
def compute_shard_key(filename: str, strategy: str = "alpha"):
    """
    Estrategias soportadas:
    - "alpha": Rango alfabético (A-M, N-Z)
    - "hash": Hash-based
    - "none": Sin sharding
    """
```

## Compatibilidad

El sistema mantiene compatibilidad hacia atrás:
- Los Processor Nodes exponen la misma API que el servidor original
- El cliente no requiere modificaciones
- El DNS sigue funcionando igual

## Diagrama de Flujo: Búsqueda

```
1. Cliente → Processor: GET /search?query=test
2. Processor → Storage PRIMARY: GET /search?query=test
   (Si falla, Circuit Breaker abre y reintenta con BACKUP)
3. Storage: Ejecuta SELECT ... WHERE name LIKE '%test%'
4. Storage → Processor: [resultados]
5. Processor → Cliente: [resultados]
```

## Diagrama de Flujo: Failover

```
1. Processor intenta conectar a Storage_1 (PRIMARY)
2. Storage_1 no responde (timeout)
3. Circuit Breaker registra fallo #1
4. Processor reintenta (hasta max_retries)
5. Después de 5 fallos, Circuit Breaker se abre
6. Processor automáticamente redirige a Storage_2 (BACKUP)
7. Después de 30s, Circuit Breaker intenta recovery
```
