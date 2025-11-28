# Informe de Sistema Distribuido - File Search
## 2da Entrega - Sistemas Distribuidos 2025

---

## Índice

1. [Arquitectura](#1-arquitectura-o-el-problema-de-cómo-diseñar-el-sistema)
2. [Procesos](#2-procesos-o-el-problema-de-cuántos-programas-o-servicios-posee-el-sistema)
3. [Comunicación](#3-comunicación-o-el-problema-de-cómo-enviar-información-mediante-la-red)
4. [Coordinación](#4-coordinación-o-el-problema-de-poner-todos-los-servicios-de-acuerdo)
5. [Nombrado y Localización](#5-nombrado-y-localización-o-el-problema-de-dónde-se-encuentra-un-recurso-y-cómo-llegar-al-mismo)
6. [Consistencia y Replicación](#6-consistencia-y-replicación-o-el-problema-de-solucionar-los-problemas-que-surgen-a-partir-de-tener-varias-copias-de-un-mismo-dato-en-el-sistema)
7. [Tolerancia a Fallos](#7-tolerancia-a-fallos-o-el-problema-de-para-qué-pasar-tanto-trabajo-distribuyendo-datos-y-servicios-si-al-fallar-una-componente-del-sistema-todo-se-viene-abajo)
8. [Seguridad](#8-seguridad-o-el-problema-de-qué-tan-vulnerable-es-su-diseño)

---

## 1. Arquitectura o el problema de cómo diseñar el sistema

### 1.1 Organización del Sistema Distribuido

El sistema **File Search** está diseñado siguiendo una arquitectura de **microservicios** desplegada sobre una infraestructura Docker Swarm. La arquitectura se organiza en tres capas principales:

```
┌─────────────────────────────────────────────────────────────────┐
│                        CAPA DE CLIENTE                          │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │              Client (Streamlit - 8501)                   │   │
│  │         Interfaz web para búsqueda de archivos           │   │
│  └─────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                     CAPA DE SERVICIOS                           │
│  ┌─────────────────────┐     ┌─────────────────────────────┐   │
│  │   DNS Service       │◀───▶│      Server (FastAPI)       │   │
│  │   (Port 5353)       │     │       (Port 8000)           │   │
│  └─────────────────────┘     └─────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                      CAPA DE DATOS                              │
│  ┌─────────────────────┐     ┌─────────────────────────────┐   │
│  │      SQLite DB      │     │     Files Volume            │   │
│  │  (Metadatos)        │     │   (Almacenamiento)          │   │
│  └─────────────────────┘     └─────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────┘
```

### 1.2 Roles del Sistema

| Rol | Componente | Descripción |
|-----|------------|-------------|
| **Cliente** | Streamlit App | Interfaz de usuario para búsqueda, visualización y descarga de archivos |
| **Servidor de Aplicación** | FastAPI Server | API REST que gestiona las operaciones CRUD sobre archivos |
| **Servicio de Nombres** | DNS Service | Resolución de nombres de servicios dentro del clúster |
| **Almacenamiento** | SQLite + Volume | Persistencia de metadatos y archivos físicos |

### 1.3 Distribución de Servicios en las Redes Docker

El sistema utiliza una red **overlay** (`file_search_net`) que permite la comunicación entre nodos del swarm:

**Nodo Manager:**
- DNS Service (1 réplica)
- Server/API (1 réplica)

**Nodo Worker:**
- Client (1 réplica)

```yaml
# Configuración de red en stack.yml
networks:
  file_search_net:
    driver: overlay
    attachable: true
```

Esta distribución asegura que los servicios críticos (DNS y API) se ejecuten en el nodo manager, mientras que la interfaz de cliente puede distribuirse en nodos workers.

---

## 2. Procesos o el problema de cuántos programas o servicios posee el sistema

### 2.1 Tipos de Procesos dentro del Sistema

El sistema cuenta con **tres procesos principales**:

| Proceso | Tipo | Tecnología | Puerto |
|---------|------|------------|--------|
| **dns_service** | Servicio de infraestructura | FastAPI + Uvicorn | 5353 |
| **server** | Servicio de aplicación | FastAPI + Uvicorn | 8000 |
| **client** | Proceso de presentación | Streamlit | 8501 |

### 2.2 Organización de los Procesos

Cada servicio se ejecuta en su propio contenedor Docker, siguiendo el principio de **un proceso por contenedor**:

```
┌─────────────────────────────────────────────────────────────┐
│ Contenedor: dns_service                                     │
│ ┌─────────────────────────────────────────────────────────┐│
│ │ Proceso Principal: uvicorn app.dns_service.main:app     ││
│ │ - Gestiona resolución de nombres                        ││
│ │ - Un solo worker para simplificar                       ││
│ └─────────────────────────────────────────────────────────┘│
└─────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────┐
│ Contenedor: server                                          │
│ ┌─────────────────────────────────────────────────────────┐│
│ │ Proceso Principal: python -m main                       ││
│ │ - Inicializa base de datos                              ││
│ │ - Sincroniza archivos al inicio                         ││
│ │ - Sirve API REST                                        ││
│ └─────────────────────────────────────────────────────────┘│
└─────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────┐
│ Contenedor: client                                          │
│ ┌─────────────────────────────────────────────────────────┐│
│ │ Proceso Principal: streamlit run client/app.py          ││
│ │ - Interfaz de usuario web                               ││
│ │ - Consultas a la API                                    ││
│ └─────────────────────────────────────────────────────────┘│
└─────────────────────────────────────────────────────────────┘
```

### 2.3 Patrón de Diseño con Respecto al Desempeño

El sistema emplea una combinación de patrones:

1. **Modelo Asíncrono (async/await)**: FastAPI utiliza programación asíncrona para manejar múltiples peticiones HTTP concurrentes sin bloquear el hilo principal.

```python
# Ejemplo en endpoints.py
@app.post("/upload")
async def upload_file_endpoint(
    request: Request,
    file: UploadFile = File(...),
    folder: str = Form(None)
):
    # Operaciones async para manejo de archivos
```

2. **Event Loop**: Uvicorn ejecuta un event loop que gestiona las conexiones de manera eficiente.

3. **Caché con TTL**: El cliente implementa un sistema de caché para las consultas a la API:

```python
@st.cache_data(ttl=10)
def fetch_files(query: str, *, limit: int, offset: int) -> List[Dict]:
    # Resultados cacheados por 10 segundos
```

4. **Caché DNS**: El DNSClient mantiene un caché local de resoluciones para reducir latencia:

```python
class DNSClient:
    def __init__(self, ...):
        self._cache: Dict[str, Dict[str, Any]] = {}
```

---

## 3. Comunicación o el problema de cómo enviar información mediante la red

### 3.1 Tipo de Comunicación

El sistema utiliza **REST (Representational State Transfer)** como protocolo principal de comunicación, implementado sobre HTTP/1.1.

**Características:**
- Comunicación sin estado (stateless)
- Intercambio de datos en formato JSON
- Métodos HTTP estándar (GET, POST, DELETE)

### 3.2 Comunicación Cliente-Servidor

```
┌──────────────┐         HTTP/REST           ┌──────────────┐
│    Client    │ ─────────────────────────▶  │    Server    │
│  (Streamlit) │         JSON                │  (FastAPI)   │
└──────────────┘ ◀─────────────────────────  └──────────────┘
```

**Endpoints principales:**

| Método | Ruta | Descripción |
|--------|------|-------------|
| GET | `/health` | Verifica estado del servicio |
| GET | `/search?query&limit&offset` | Búsqueda paginada de archivos |
| GET | `/files` | Lista todos los archivos |
| GET | `/files/{file_id}/download` | Descarga un archivo |
| POST | `/files` | Registra/actualiza un archivo |
| POST | `/upload` | Sube un nuevo archivo |
| DELETE | `/files/{file_id}` | Elimina un archivo |

### 3.3 Comunicación Servidor-Servidor (DNS Service)

```
┌──────────────┐         HTTP/REST           ┌──────────────┐
│    Client    │ ─────────────────────────▶  │  DNS Service │
│  (Resolver)  │                             │   (FastAPI)  │
└──────────────┘ ◀─────────────────────────  └──────────────┘
                   ResolutionResponse
```

**Endpoint DNS:**

```python
@app.get("/resolve/{hostname}")
def resolve_hostname(hostname: str) -> ResolutionResponse:
    return ResolutionResponse(
        hostname=hostname,
        ip=ip_address,
        ttl=300
    )
```

### 3.4 Comunicación entre Procesos

La comunicación interna dentro de cada servicio sigue un patrón de llamadas directas a funciones:

```
┌─────────────────────────────────────────────────────────────┐
│                         SERVER                               │
│                                                              │
│  ┌─────────────┐    ┌─────────────┐    ┌─────────────────┐  │
│  │  endpoints  │───▶│    crud     │───▶│     SQLite      │  │
│  │   (API)     │    │   (Data)    │    │   (Storage)     │  │
│  └─────────────┘    └─────────────┘    └─────────────────┘  │
│         │                                                    │
│         ▼                                                    │
│  ┌─────────────┐                                            │
│  │   scanner   │                                            │
│  │ (Sync)      │                                            │
│  └─────────────┘                                            │
└─────────────────────────────────────────────────────────────┘
```

---

## 4. Coordinación o el problema de poner todos los servicios de acuerdo

### 4.1 Sincronización de Acciones

El sistema implementa sincronización en los siguientes escenarios:

1. **Inicialización del Servidor**: Al arrancar, el servidor sincroniza la base de datos con el sistema de archivos:

```python
@app.on_event("startup")
async def startup_event():
    init_db()
    summary = sync()
    logger.info("Base de datos inicializada y sincronizada", extra={"sync": summary})
```

2. **Orden de Arranque de Servicios**: Docker Compose asegura el orden de inicio mediante `depends_on`:

```yaml
services:
  server:
    depends_on:
      - dns_service
  
  client:
    depends_on:
      - server
      - dns_service
```

### 4.2 Acceso Exclusivo a Recursos - Condiciones de Carrera

El sistema gestiona el acceso a recursos compartidos de la siguiente manera:

1. **Base de Datos SQLite**: Se utiliza un timeout de conexión para evitar bloqueos:

```python
conn = sqlite3.connect(str(DB_PATH), timeout=30, check_same_thread=False)
```

2. **Operaciones Atómicas con UPSERT**: Para evitar condiciones de carrera en la inserción/actualización:

```sql
INSERT INTO files (file_id, name, path, size, last_modified)
VALUES (?, ?, ?, ?, ?)
ON CONFLICT(file_id) DO UPDATE SET
    name = excluded.name,
    path = excluded.path,
    size = excluded.size,
    last_modified = excluded.last_modified
```

3. **Context Manager para Transacciones**:

```python
@contextmanager
def get_conn():
    conn = sqlite3.connect(...)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
```

### 4.3 Toma de Decisiones Distribuidas

En el estado actual del sistema (versión centralizada), la toma de decisiones se realiza de forma local en cada servicio. Para la evolución hacia un sistema distribuido completo, se planifica:

1. **Descubrimiento de Servicios**: El DNS Service actúa como punto de coordinación para localizar servicios activos.

2. **Política de Fallback**: El cliente implementa fallback automático cuando el DNS Service no está disponible:

```python
def resolve(self, hostname: str) -> str:
    # 1. Verificar Caché
    # 2. Consultar API del DNS Service
    # 3. Fallback: Resolución nativa (Docker DNS)
```

---

## 5. Nombrado y Localización o el problema de dónde se encuentra un recurso y cómo llegar al mismo

### 5.1 Identificación de Datos y Servicios

**Identificación de Archivos:**

Los archivos se identifican mediante un hash SHA-1 de su ruta relativa:

```python
def _compute_file_id(relative_path: str) -> str:
    digest = hashlib.sha1(relative_path.encode("utf-8"))
    return digest.hexdigest()
```

**Identificación de Servicios:**

| Servicio | Nombre DNS | Puerto |
|----------|------------|--------|
| API Server | `server` | 8000 |
| DNS Service | `dns_service` | 5353 |
| Client | `client` | 8501 |

### 5.2 Ubicación de Datos y Servicios

**Ubicación de Datos:**
- **Metadatos**: Base de datos SQLite en `/app/server/db/doc_search.db`
- **Archivos físicos**: Volumen montado en `/app/files`
- **Logs**: Directorio `/app/logs`

**Ubicación de Servicios:**
- Nodo Manager: `dns_service`, `server`
- Nodo Worker: `client`

### 5.3 Localización de Datos y Servicios

El sistema implementa un **servicio DNS personalizado** para la localización de servicios:

```
┌─────────────┐        ┌─────────────┐        ┌─────────────┐
│   Client    │───────▶│ DNS Service │───────▶│   Docker    │
│             │ HTTP   │             │ Socket │    DNS      │
│             │◀───────│             │◀───────│  127.0.0.11 │
└─────────────┘  IP    └─────────────┘  IP    └─────────────┘
```

**Flujo de Resolución:**

1. El cliente solicita la IP del servicio `server` al DNS Service
2. El DNS Service consulta el DNS interno de Docker
3. El DNS Service devuelve la IP resuelta con un TTL
4. El cliente cachea la respuesta y la utiliza para comunicarse

```python
class DNSClient:
    def resolve(self, hostname: str) -> str:
        # 1. Revisa caché local (TTL)
        # 2. Consulta al DNS Service vía API
        # 3. Fallback a resolución nativa
```

---

## 6. Consistencia y Replicación o el problema de solucionar los problemas que surgen a partir de tener varias copias de un mismo dato en el sistema

### 6.1 Distribución de los Datos

Actualmente, el sistema utiliza una arquitectura de datos centralizada:

```
┌─────────────────────────────────────────────────────────────┐
│                    NODO PRINCIPAL                            │
│                                                              │
│  ┌───────────────────┐      ┌───────────────────────────┐   │
│  │     SQLite DB     │      │       File Volume         │   │
│  │  (Única instancia)│      │   (Almacenamiento único)  │   │
│  └───────────────────┘      └───────────────────────────┘   │
│                                                              │
└─────────────────────────────────────────────────────────────┘
```

**Estrategia planificada para replicación:**
- Implementación de múltiples réplicas del servidor API
- Sincronización de base de datos entre réplicas
- Volumen compartido NFS para archivos

### 6.2 Replicación - Cantidad de Réplicas

**Configuración actual en stack.yml:**

```yaml
deploy:
  replicas: 1  # Una réplica por servicio
  restart_policy:
    condition: on-failure
```

**Objetivo de tolerancia a fallos nivel 2:**
- Mínimo 2 réplicas del servidor API
- DNS Service con réplica de respaldo
- Cliente escalable horizontalmente

### 6.3 Confiabilidad de las Réplicas tras Actualización

Para mantener la consistencia, el sistema implementa:

1. **Sincronización al inicio**: Cada servidor sincroniza su estado con el filesystem al arrancar:

```python
@app.on_event("startup")
async def startup_event():
    init_db()
    summary = sync()
```

2. **Identificadores estables**: Los file_id basados en hash garantizan identificación consistente:

```python
def _compute_file_id(relative_path: str) -> str:
    return hashlib.sha1(relative_path.encode("utf-8")).hexdigest()
```

3. **Operaciones idempotentes**: UPSERT permite reintentos sin duplicación.

---

## 7. Tolerancia a Fallos o el problema de para qué pasar tanto trabajo distribuyendo datos y servicios si al fallar una componente del sistema todo se viene abajo

### 7.1 Respuesta a Errores

El sistema implementa múltiples mecanismos de respuesta a errores:

**1. Manejo de excepciones HTTP:**

```python
@app.get("/files/{file_id}/download")
def download_file_endpoint(file_id: str, request: Request):
    try:
        target = file_handler.resolve_download(file_id)
    except file_handler.FileRecordNotFoundError:
        raise HTTPException(status_code=404, detail="Archivo no encontrado")
    except file_handler.FileOnDiskNotFoundError:
        raise HTTPException(status_code=404, detail="Archivo no disponible en disco")
```

**2. Fallback en resolución DNS:**

```python
def resolve(self, hostname: str) -> str:
    # Si falla el DNS Service, usa resolución nativa
    try:
        ip = socket.gethostbyname(hostname)
        return ip
    except socket.gaierror:
        logger.error(f"Imposible resolver '{hostname}' incluso con fallback.")
        raise
```

**3. Re-bootstrap automático:**

```python
if self.dns_ip is None:
    logger.warning("DNS Service IP no disponible. Intentando re-bootstrap...")
    self._bootstrap()
```

### 7.2 Nivel de Tolerancia a Fallos Esperado

**Objetivo: Nivel 2 de tolerancia a fallos**

| Componente | Estado Actual | Objetivo |
|------------|---------------|----------|
| DNS Service | 1 réplica | 2 réplicas + fallback |
| API Server | 1 réplica | 2+ réplicas con load balancing |
| Client | 1 réplica | Escalable según demanda |
| Base de Datos | 1 instancia | Replicación master-slave |

### 7.3 Fallos Parciales - Nodos Caídos Temporalmente

**Mecanismos implementados:**

1. **Restart automático en Docker:**

```yaml
deploy:
  restart_policy:
    condition: on-failure
```

2. **Timeouts en conexiones:**

```python
response = requests.get(url, timeout=2.0)
```

3. **Caché con TTL reducido para fallback:**

```python
# Cachear resultado de fallback por menos tiempo
self._cache[hostname] = {
    'ip': ip,
    'expires_at': time.time() + 60  # 1 minuto para fallback
}
```

**Nodos nuevos que se incorporan:**

- Los nuevos contenedores se registran automáticamente en el DNS de Docker
- El DNS Service puede resolver nuevos servicios inmediatamente
- La sincronización de archivos ocurre al inicio de cada servidor

---

## 8. Seguridad o el problema de qué tan vulnerable es su diseño

### 8.1 Seguridad con Respecto a la Comunicación

**Estado actual:**
- Comunicación HTTP sin cifrado dentro del clúster Docker
- Red overlay aislada del exterior
- Puertos expuestos controlados por el firewall de Docker

**Mejoras planificadas:**
- Implementación de HTTPS con certificados TLS
- Mutual TLS (mTLS) para comunicación entre servicios
- API Gateway con rate limiting

### 8.2 Seguridad con Respecto al Diseño

**Medidas implementadas:**

1. **Aislamiento de contenedores**: Cada servicio corre en su propio contenedor con recursos limitados.

2. **Red overlay privada**: Los servicios se comunican en una red interna no accesible externamente.

3. **Validación de entrada con Pydantic:**

```python
class FileIn(BaseModel):
    file_id: str
    name: str
    path: str
    size: int
    last_modified: datetime
```

4. **Manejo seguro de archivos:**

```python
# Control de directorios de destino
target_dir = _DEFAULT_ROOT
if folder:
    target_dir = target_dir / folder
    os.makedirs(target_dir, exist_ok=True)
```

**Vulnerabilidades conocidas y mitigaciones planificadas:**

| Vulnerabilidad | Riesgo | Mitigación Planificada |
|----------------|--------|------------------------|
| SQL Injection | Bajo (usa parametrización) | Auditoría de queries |
| Path Traversal | Medio | Validación de rutas |
| DoS | Medio | Rate limiting, quotas |

### 8.3 Autorización y Autenticación

**Estado actual:**
- Sin autenticación implementada
- Acceso abierto a todos los endpoints

**Mejoras planificadas para cumplir requisitos adicionales:**

1. **Autenticación JWT:**
   - Tokens de acceso con tiempo de expiración
   - Refresh tokens para renovación

2. **Roles y permisos:**
   - Usuario: lectura y descarga
   - Administrador: upload y eliminación

3. **Auditoría de accesos:**
   - Logging estructurado ya implementado
   - Access logs separados de application logs

```python
access_logger = logging.getLogger("app.access")
access_logger.info(
    "%s %s %s -> descarga '%s'",
    client_host,
    request.method,
    request.url.path,
    target.filename,
)
```

---

## Diagrama de Despliegue

```
┌─────────────────────────────────────────────────────────────────┐
│                     ORDENADOR 1 (Manager)                        │
│  ┌─────────────────────────────────────────────────────────┐    │
│  │                  Docker Swarm Manager                    │    │
│  │  ┌─────────────────┐    ┌─────────────────────────────┐ │    │
│  │  │   dns_service   │    │          server             │ │    │
│  │  │   Port: 5353    │◀──▶│        Port: 8000           │ │    │
│  │  └─────────────────┘    └─────────────────────────────┘ │    │
│  │                              │                          │    │
│  │                    ┌─────────┴─────────┐                │    │
│  │                    │                   │                │    │
│  │               ┌────▼────┐        ┌─────▼─────┐          │    │
│  │               │ SQLite  │        │  /files   │          │    │
│  │               │   DB    │        │  Volume   │          │    │
│  │               └─────────┘        └───────────┘          │    │
│  └─────────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────────┘
                              │
                    Red Overlay (file_search_net)
                              │
┌─────────────────────────────────────────────────────────────────┐
│                     ORDENADOR 2 (Worker)                         │
│  ┌─────────────────────────────────────────────────────────┐    │
│  │                  Docker Swarm Worker                     │    │
│  │  ┌─────────────────────────────────────────────────────┐│    │
│  │  │                    client                           ││    │
│  │  │                  Port: 8501                         ││    │
│  │  │               (Streamlit UI)                        ││    │
│  │  └─────────────────────────────────────────────────────┘│    │
│  └─────────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────────┘
```

---

## Conclusiones

El sistema **File Search** implementa una arquitectura de microservicios sobre Docker Swarm que permite:

1. **Escalabilidad**: Los servicios pueden replicarse según la demanda.
2. **Aislamiento**: Cada componente opera de forma independiente.
3. **Descubrimiento dinámico**: El DNS Service facilita la localización de servicios.
4. **Resiliencia**: Mecanismos de fallback y restart automático.

**Próximos pasos para alcanzar tolerancia a fallos nivel 2:**
- Implementar replicación de la base de datos
- Configurar múltiples réplicas del servidor API
- Añadir load balancer para distribución de carga
- Implementar sincronización de datos entre réplicas

---

*Documento generado para la 2da Entrega de Sistemas Distribuidos - Curso 2025*
