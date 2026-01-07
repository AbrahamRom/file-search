# 🔐 Soluciones de Seguridad - Ordenadas por Complejidad y Tiempo

**Fecha:** 7 de enero de 2026  
**Sistema:** File Search - Arquitectura Distribuida  
**Nivel de Urgencia:** CRÍTICO

---

## 📊 Orden de Implementación (De Más Fácil a Más Complejo)

---

## **1️⃣ SANITIZAR LOGS - Quitar Información Sensible**

**⏱️ Tiempo:** 5-10 minutos  
**Dificultad:** ⭐ Muy Fácil  
**Impacto de Seguridad:** 🟡 Medio

### Descripción
Los logs actuales contienen información sensible como queries de búsqueda. Simplemente necesitas comentar líneas que expongan datos.

### Cambios Requeridos

**Archivo:** `app/storage/api/endpoints.py`

```python
# ANTES (❌ Inseguro)
access_logger.info(
    "%s GET /search?query=%s -> %d results",
    client_host,
    query,  # ❌ Query sensible en logs públicos
    len(results),
)

# DESPUÉS (✅ Seguro)
access_logger.info(
    "%s GET /search -> %d results",
    client_host,
    len(results),
)
```

**Archivo:** `app/processor/api/endpoints.py`

```python
# ANTES (❌ Inseguro)
access_logger.info(
    "%s %s %s -> Status: %s",
    client_host,
    method,
    path,
    status_code,
)

# DESPUÉS (✅ Seguro - no incluir query params)
access_logger.info(
    "%s %s -> Status: %s",
    client_host,
    path.split("?")[0],  # Solo la ruta, sin parámetros
    status_code,
)
```

### Implementación
- Buscar todos los `access_logger.info()` que mencionen `query` o datos sensibles
- Remover esos campos de los logs
- Mantener solo: IP, método HTTP, ruta, código de estado

### Validación
```bash
# Verificar que los logs no contienen queries
grep -r "query=" app/*/api/endpoints.py
# Debería estar vacío o no existir en logs
```

---

## **2️⃣ CORS RESTRICTIVO - Limitar Orígenes Permitidos**

**⏱️ Tiempo:** 5-8 minutos  
**Dificultad:** ⭐ Muy Fácil  
**Impacto de Seguridad:** 🔴 Crítico

### Descripción
Cambiar `allow_origins=["*"]` por dominios específicos en lugar de permitir desde cualquier lugar.

### Cambios Requeridos

**Archivo:** `app/storage/api/endpoints.py`

Buscar esta sección:
```python
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # ❌ CRÍTICO: Permite desde cualquier dominio
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition"],
)
```

Reemplazar por:
```python
# Configuración de CORS segura
ALLOWED_ORIGINS = [
    "http://localhost:8501",      # Desarrollo local
    "http://127.0.0.1:8501",      # Loopback
    "http://client:8501",         # Dentro de Docker
]

# Permitir más orígenes si es necesario (cambiar según tu ambiente):
if os.getenv("ENVIRONMENT") == "production":
    ALLOWED_ORIGINS = [
        "https://myapp.example.com",  # Tu dominio en producción
    ]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,  # ✅ Solo orígenes permitidos
    allow_credentials=True,
    allow_methods=["GET", "POST", "DELETE"],  # Solo métodos necesarios
    allow_headers=["Content-Type", "Authorization"],
    expose_headers=["Content-Disposition"],
)
```

**Archivo:** `app/processor/api/endpoints.py`

Aplicar la misma configuración si existe CORS allí.

### Validación
```bash
# Verificar que CORS está configurado correctamente
curl -H "Origin: http://malicious.com" \
     -H "Access-Control-Request-Method: GET" \
     http://localhost:8000/health
# Debería NO incluir header Access-Control-Allow-Origin
```

---

## **3️⃣ CORRER CONTENEDORES SIN ROOT**

**⏱️ Tiempo:** 8-12 minutos  
**Dificultad:** ⭐ Fácil  
**Impacto de Seguridad:** 🟠 Alto

### Descripción
Actualmente todos los contenedores corren como `root`. Si alguien infiltra un contenedor, tendrá acceso total. Crear un usuario no-privilegiado.

### Cambios Requeridos

**Archivo:** `Dockerfile.processor`

Agregar al final antes de `ENTRYPOINT`:

```dockerfile
# ... resto del Dockerfile ...

# Crear usuario no-privilegiado
RUN useradd -m -u 1000 appuser && \
    chown -R appuser:appuser /app

# Cambiar a usuario no-privilegiado
USER appuser

EXPOSE 8000

ENTRYPOINT ["/app/entrypoint.sh"]
```

**Archivo:** `Dockerfile.storage`

Mismo cambio:

```dockerfile
# Crear usuario no-privilegiado
RUN useradd -m -u 1000 appuser && \
    chown -R appuser:appuser /app && \
    chown -R appuser:appuser /tmp/source_files

USER appuser

EXPOSE 8000

ENTRYPOINT ["/app/entrypoint.sh"]
```

**Archivo:** `Dockerfile.client`

Mismo cambio:

```dockerfile
# Crear usuario no-privilegiado
RUN useradd -m -u 1000 appuser && \
    chown -R appuser:appuser /app

USER appuser

EXPOSE 8501

CMD ["streamlit", "run", "client/app.py", ...]
```

### Validación
```bash
# Verificar usuario en contenedor
docker exec <container_id> id
# Debería mostrar: uid=1000(appuser) gid=1000(appuser) groups=1000(appuser)
```

---

## **4️⃣ VALIDAR PATH TRAVERSAL - Prevenir Acceso a Archivos del Sistema**

**⏱️ Tiempo:** 10-15 minutos  
**Dificultad:** ⭐ Fácil  
**Impacto de Seguridad:** 🔴 Crítico

### Descripción
Alguien podría usar `../../../etc/passwd` para acceder a archivos fuera del directorio permitido. Validar que el file_id es seguro.

### Cambios Requeridos

**Archivo:** `app/storage/services/file_handler.py`

Buscar la función `resolve_download()`:

```python
# ANTES (❌ Vulnerable)
def resolve_download(file_id: str):
    target = Path(FILES_ROOT) / file_id
    # ... simplemente usa file_id sin validar
    return target

# DESPUÉS (✅ Seguro)
def resolve_download(file_id: str):
    """Resolver descarga validando que no hay path traversal."""
    from pathlib import Path
    
    # 1. Validar que file_id no contiene caracteres peligrosos
    if ".." in file_id or file_id.startswith("/"):
        raise FileRecordNotFoundError(f"Invalid file_id: {file_id}")
    
    # 2. Resolver rutas
    base_path = Path(FILES_ROOT).resolve()
    target_path = (base_path / file_id).resolve()
    
    # 3. Validar que target está dentro de FILES_ROOT
    try:
        target_path.relative_to(base_path)
    except ValueError:
        # Path está fuera de FILES_ROOT
        raise FileRecordNotFoundError(f"Path traversal attempt: {file_id}")
    
    # ... resto del código
    return target_path
```

### Validación
```bash
# Intentar path traversal - debería fallar
curl http://localhost:8000/files/../../../etc/passwd/download
# Respuesta: 404 Not Found (correcto)

# Acceso válido - debería funcionar
curl http://localhost:8000/files/valid-file-id/download
```

---

## **5️⃣ LIMITACIONES EN ENTRADA - Max Length y Máximos**

**⏱️ Tiempo:** 12-18 minutos  
**Dificultad:** ⭐ Fácil  
**Impacto de Seguridad:** 🟡 Medio (Previene DoS)

### Descripción
La query de búsqueda puede ser gigante (DoS), y el offset sin límite puede causar búsquedas infinitas. Agregar límites.

### Cambios Requeridos

**Archivo:** `app/storage/api/endpoints.py`

Buscar la función `search_files_endpoint()`:

```python
# ANTES (❌ Sin límites)
@app.get("/search", response_model=SearchResponse)
def search_files_endpoint(
    request: Request,
    query: str = Query(..., min_length=1),  # ❌ Sin max_length
    limit: int = Query(10, ge=1, le=100),
    offset: int = Query(0, ge=0),  # ❌ Sin máximo
    shard_id: Optional[str] = Query(None),
):

# DESPUÉS (✅ Con límites)
@app.get("/search", response_model=SearchResponse)
def search_files_endpoint(
    request: Request,
    query: str = Query(..., min_length=1, max_length=500),  # ✅ Máximo 500 caracteres
    limit: int = Query(10, ge=1, le=100),
    offset: int = Query(0, ge=0, le=1000000),  # ✅ Máximo offset
    shard_id: Optional[str] = Query(None, max_length=100),  # ✅ Si existe
):
```

**Archivo:** `app/processor/api/endpoints.py`

Mismo cambio en los endpoints de búsqueda:

```python
@app.get("/search", response_model=SearchResponse)
async def search(
    query: str = Query(..., min_length=1, max_length=500),  # ✅ Agregar max_length
    limit: int = Query(10, ge=1, le=100),
    offset: int = Query(0, ge=0, le=1000000),  # ✅ Agregar máximo
):
```

### Validación
```bash
# Query muy larga - debería rechazarse
curl "http://localhost:8000/search?query=$(python3 -c 'print(\"a\"*1000)')"
# Respuesta: 422 Unprocessable Entity (correcto)

# Query normal - debería funcionar
curl "http://localhost:8000/search?query=test"
```

---

## **6️⃣ SECRETS MANAGEMENT - Variables de Entorno Seguras**

**⏱️ Tiempo:** 15-20 minutos  
**Dificultad:** ⭐⭐ Fácil-Medio  
**Impacto de Seguridad:** 🟠 Alto

### Descripción
No hardcodear secrets en el código. Usar variables de entorno que se carguen de archivos `.env` seguros.

### Cambios Requeridos

**Crear archivo:** `app/.env.example`

```bash
# Autenticación
AUTH_TOKEN=your-secret-token-here
DNS_SHARED_SECRET=your-dns-secret-here

# Base de datos
DB_PASSWORD=your-db-password

# Configuración
ENVIRONMENT=development
LOG_LEVEL=INFO
```

**Crear archivo:** `app/.env` (en .gitignore - NO versionar)

```bash
AUTH_TOKEN=change-me-in-production
DNS_SHARED_SECRET=change-me-in-production
ENVIRONMENT=development
```

**Archivo:** `app/processor/api/endpoints.py`

Agregar al inicio:

```python
import os
from dotenv import load_dotenv

# Cargar variables de entorno
load_dotenv()

# Configuración de autenticación
AUTH_TOKEN = os.getenv("AUTH_TOKEN")
if not AUTH_TOKEN:
    logger.warning("AUTH_TOKEN no configurado. Usando valor por defecto (INSEGURO)")
    AUTH_TOKEN = "default-change-me"
```

**Archivo:** `app/storage/api/endpoints.py`

Mismo cambio:

```python
import os
from dotenv import load_dotenv

load_dotenv()

AUTH_TOKEN = os.getenv("AUTH_TOKEN")
if not AUTH_TOKEN:
    logger.warning("AUTH_TOKEN no configurado")
    AUTH_TOKEN = "default-change-me"
```

**Archivo:** `app/dns_service/main.py`

```python
import os
from dotenv import load_dotenv

load_dotenv()

DNS_SHARED_SECRET = os.getenv("DNS_SHARED_SECRET", "default-secret-change-me")
```

### Agregar a requirements.txt

En todos los `requirements.txt`:

```
python-dotenv==1.0.0
```

### Actualizar .gitignore

```bash
.env
.env.local
*.pyc
__pycache__/
.DS_Store
```

### Validación
```bash
# Verificar que .env existe
ls -la app/.env

# Verificar que variables se cargan correctamente
python3 -c "from dotenv import load_dotenv; import os; load_dotenv(); print(os.getenv('AUTH_TOKEN'))"
```

---

## **7️⃣ RATE LIMITING - Prevenir Abuso de Endpoints**

**⏱️ Tiempo:** 18-25 minutos  
**Dificultad:** ⭐⭐ Fácil-Medio  
**Impacto de Seguridad:** 🟡 Medio (Previene DoS)

### Descripción
Sin límites de rate, alguien puede bombardear con 1000s de requests/segundo. Implementar throttling por IP.

### Cambios Requeridos

**Agregar a requirements.txt (todos los servicios):**

```
slowapi==0.1.9
```

**Archivo:** `app/processor/api/endpoints.py`

Agregar al inicio del archivo (después de imports):

```python
from slowapi import Limiter
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

# Configurar limitador
limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter

# Handler para errores de rate limit
@app.exception_handler(RateLimitExceeded)
async def ratelimit_handler(request, exc):
    return {
        "detail": "Too many requests. Try again later.",
        "retry_after": exc.headers.get("retry-after", "60")
    }
```

Luego en cada endpoint importante:

```python
# ANTES
@app.get("/search", response_model=SearchResponse)
async def search(query: str, ...):

# DESPUÉS
@app.get("/search", response_model=SearchResponse)
@limiter.limit("30/minute")  # 30 requests por minuto por IP
async def search(request: Request, query: str, ...):
    # Agregar request como primer parámetro para que slowapi funcione
```

Aplicar a endpoints críticos:

```python
@limiter.limit("30/minute")
@app.get("/search")
async def search(request: Request, ...): ...

@limiter.limit("10/minute")  # Upload menos frecuente
@app.post("/files/upload")
async def upload(request: Request, ...): ...

@limiter.limit("20/minute")
@app.get("/files/{file_id}/download")
async def download(request: Request, ...): ...
```

**Archivo:** `app/storage/api/endpoints.py`

Mismo cambio:

```python
from slowapi import Limiter
from slowapi.util import get_remote_address

limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter

@limiter.limit("30/minute")
@app.get("/search")
async def search_files_endpoint(request: Request, ...): ...
```

### Validación
```bash
# Hacer múltiples requests rápidos
for i in {1..40}; do
  curl http://localhost:8000/search?query=test
done
# Después de 30, debería recibir: 429 Too Many Requests
```

---

## **8️⃣ AUTENTICACIÓN CON TOKENS - Proteger Todos los Endpoints**

**⏱️ Tiempo:** 25-35 minutos  
**Dificultad:** ⭐⭐ Medio  
**Impacto de Seguridad:** 🔴 Crítico

### Descripción
Implementar autenticación básica con tokens Bearer. Todos los endpoints requieren un header `Authorization: Bearer <token>`.

### Cambios Requeridos

**Crear archivo:** `app/security/__init__.py`

```python
# Archivo vacío para que sea paquete
```

**Crear archivo:** `app/security/auth.py`

```python
"""Autenticación y autorización."""

import os
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthCredentials
import logging

logger = logging.getLogger(__name__)

security = HTTPBearer()

# Obtener tokens válidos desde variables de entorno
def get_valid_tokens() -> set:
    """Obtener tokens válidos desde configuración."""
    auth_token = os.getenv("AUTH_TOKEN", "change-me-in-production")
    # En producción, podría ser una lista separada por comas
    tokens = [auth_token]
    return set(tokens)

async def verify_token(credentials: HTTPAuthCredentials = Depends(security)) -> str:
    """
    Verificar que el token es válido.
    
    Uso:
        @app.get("/search")
        async def search(token: str = Depends(verify_token)):
            ...
    """
    valid_tokens = get_valid_tokens()
    
    if credentials.credentials not in valid_tokens:
        logger.warning(f"Intento de acceso con token inválido: {credentials.credentials[:10]}...")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    return credentials.credentials
```

**Archivo:** `app/processor/api/endpoints.py`

Agregar import:

```python
from app.security.auth import verify_token
```

Modificar endpoints para requieren autenticación:

```python
# ANTES
@app.get("/search", response_model=SearchResponse)
async def search(query: str, ...):

# DESPUÉS
@app.get("/search", response_model=SearchResponse)
async def search(
    token: str = Depends(verify_token),  # ← Agregar esto
    query: str = Query(..., min_length=1, max_length=500),
    ...
):
```

Aplicar a TODOS los endpoints públicos:

```python
@app.get("/files/{file_id}/download")
async def download_file(
    token: str = Depends(verify_token),
    file_id: str = ...,
):
    ...

@app.post("/files/upload")
async def upload_file(
    token: str = Depends(verify_token),
    file: UploadFile = File(...),
):
    ...
```

**Archivo:** `app/storage/api/endpoints.py`

Mismo cambio (agregar `token` dependency a todos los endpoints).

**Archivo:** `app/dns_service/main.py`

Proteger endpoints de registro:

```python
from fastapi import Header, HTTPException

DNS_SHARED_SECRET = os.getenv("DNS_SHARED_SECRET", "default-secret-change-me")

@app.post("/processor/register")
async def register_processor(
    x_secret: str = Header(None),
    data: dict = ...
):
    if x_secret != DNS_SHARED_SECRET:
        raise HTTPException(status_code=403, detail="Invalid secret")
    
    # ... resto del código

@app.post("/storage/register")
async def register_storage(
    x_secret: str = Header(None),
    data: dict = ...
):
    if x_secret != DNS_SHARED_SECRET:
        raise HTTPException(status_code=403, detail="Invalid secret")
    
    # ... resto del código
```

### Actualizar cliente para usar tokens

**Archivo:** `app/client/app.py`

Agregar token a todas las requests:

```python
# En la función que hace requests
def make_request(method, endpoint, **kwargs):
    token = os.getenv("AUTH_TOKEN", "change-me")
    headers = kwargs.get("headers", {})
    headers["Authorization"] = f"Bearer {token}"
    kwargs["headers"] = headers
    
    response = requests.request(method, endpoint, **kwargs)
    return response
```

O si usas requests directamente:

```python
import os

AUTH_TOKEN = os.getenv("AUTH_TOKEN", "change-me")
HEADERS = {"Authorization": f"Bearer {AUTH_TOKEN}"}

# En cada request:
response = requests.get(url, headers=HEADERS)
```

### Actualizar Docker Compose

**Archivo:** `stack.separated.yml` o `docker-compose.yml`

```yaml
processor_1:
  environment:
    - AUTH_TOKEN=${AUTH_TOKEN:-change-me-in-production}
    - DNS_SHARED_SECRET=${DNS_SHARED_SECRET:-change-me}

storage_1:
  environment:
    - AUTH_TOKEN=${AUTH_TOKEN:-change-me-in-production}
    - DNS_SHARED_SECRET=${DNS_SHARED_SECRET:-change-me}

dns_1:
  environment:
    - DNS_SHARED_SECRET=${DNS_SHARED_SECRET:-change-me}
```

### Validación
```bash
# Sin token - debería fallar
curl http://localhost:8000/search?query=test
# Respuesta: 403 Forbidden o 401 Unauthorized

# Con token correcto - debería funcionar
curl -H "Authorization: Bearer change-me-in-production" \
     http://localhost:8000/search?query=test

# Con token incorrecto - debería fallar
curl -H "Authorization: Bearer wrong-token" \
     http://localhost:8000/search?query=test
# Respuesta: 401 Unauthorized
```

---

## **9️⃣ HTTPS/TLS - Encriptación en Tránsito**

**⏱️ Tiempo:** 45-60 minutos  
**Dificultad:** ⭐⭐⭐ Medio-Complejo  
**Impacto de Seguridad:** 🔴 Crítico

### Descripción
Actualmente todo es HTTP sin encriptación. Implementar HTTPS con certificados TLS (auto-signed para desarrollo, Let's Encrypt para producción).

### Paso 1: Generar Certificados Auto-Firmados

```bash
# En la raíz del proyecto
mkdir -p certs

# Generar certificado y clave privada (365 días)
openssl req -x509 -newkey rsa:4096 -nodes \
  -out certs/cert.pem \
  -keyout certs/key.pem \
  -days 365 \
  -subj "/C=ES/ST=State/L=City/O=Organization/CN=localhost"
```

### Paso 2: Actualizar FastAPI para usar HTTPS

**Crear archivo:** `app/processor/ssl_config.py`

```python
"""Configuración SSL/TLS."""

import os
import ssl

def get_ssl_context():
    """Crear contexto SSL para HTTPS."""
    cert_file = os.getenv("SSL_CERT_FILE", "/app/certs/cert.pem")
    key_file = os.getenv("SSL_KEY_FILE", "/app/certs/key.pem")
    
    if not os.path.exists(cert_file) or not os.path.exists(key_file):
        # Si no existen certificados, retornar None (HTTP)
        return None
    
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    context.load_cert_chain(cert_file, key_file)
    return context
```

**Archivo:** `app/processor/main.py`

Modificar la sección de ejecución:

```python
# ANTES
if __name__ == "__main__":
    try:
        uvicorn = importlib.import_module("uvicorn")
    except ModuleNotFoundError as exc:
        raise SystemExit(
            "uvicorn is not installed. Run `pip install uvicorn`."
        ) from exc
    
    host, port = _get_server_config()
    
    uvicorn.run(
        "processor.api.endpoints:app",
        host=host,
        port=port,
        reload=False,
    )

# DESPUÉS
if __name__ == "__main__":
    try:
        uvicorn = importlib.import_module("uvicorn")
    except ModuleNotFoundError as exc:
        raise SystemExit(
            "uvicorn is not installed. Run `pip install uvicorn`."
        ) from exc
    
    host, port = _get_server_config()
    
    # Configuración SSL
    ssl_context = None
    use_https = os.getenv("USE_HTTPS", "false").lower() == "true"
    
    if use_https:
        from .ssl_config import get_ssl_context
        ssl_context = get_ssl_context()
        if ssl_context:
            logger.info("HTTPS enabled with SSL certificates")
        else:
            logger.warning("HTTPS requested but certificates not found. Using HTTP")
    
    uvicorn.run(
        "processor.api.endpoints:app",
        host=host,
        port=port,
        ssl_certfile="certs/cert.pem" if use_https else None,
        ssl_keyfile="certs/key.pem" if use_https else None,
        reload=False,
    )
```

**Archivo:** `app/storage/main.py`

Mismo cambio:

```python
# Agregar al final:
if __name__ == "__main__":
    # ... código existente ...
    
    use_https = os.getenv("USE_HTTPS", "false").lower() == "true"
    
    uvicorn.run(
        "storage.api.endpoints:app",
        host=host,
        port=port,
        ssl_certfile="certs/cert.pem" if use_https else None,
        ssl_keyfile="certs/key.pem" if use_https else None,
        reload=False,
    )
```

### Paso 3: Actualizar Dockerfiles

**Archivo:** `Dockerfile.processor`

Agregar al final:

```dockerfile
# ... resto del Dockerfile ...

# Copiar certificados SSL/TLS (si existen)
COPY certs /app/certs 2>/dev/null || true

# Configuración HTTPS
ENV USE_HTTPS=false
ENV SSL_CERT_FILE=/app/certs/cert.pem
ENV SSL_KEY_FILE=/app/certs/key.pem

EXPOSE 8000

USER appuser

ENTRYPOINT ["/app/entrypoint.sh"]
```

**Archivo:** `Dockerfile.storage`

Mismo cambio.

**Archivo:** `Dockerfile.client`

Mismo cambio.

### Paso 4: Actualizar Docker Compose

**Archivo:** `stack.separated.yml`

```yaml
processor_1:
  image: file-search-processor:latest
  environment:
    - USE_HTTPS=true  # ← Activar HTTPS
    - SSL_CERT_FILE=/app/certs/cert.pem
    - SSL_KEY_FILE=/app/certs/key.pem
  volumes:
    - ./certs:/app/certs:ro  # ← Montar certificados
    - ${LOGS_SOURCE:-/srv/file-search/logs}:/app/logs

storage_1:
  image: file-search-storage:latest
  environment:
    - USE_HTTPS=true
    - SSL_CERT_FILE=/app/certs/cert.pem
    - SSL_KEY_FILE=/app/certs/key.pem
  volumes:
    - ./certs:/app/certs:ro
    - ${LOGS_SOURCE:-/srv/file-search/logs}:/app/logs
```

### Paso 5: Actualizar URLs en Cliente

**Archivo:** `app/processor/services/storage_client.py`

```python
# ANTES
storage_url = "http://storage_1:8000"

# DESPUÉS
use_https = os.getenv("USE_HTTPS", "false").lower() == "true"
protocol = "https" if use_https else "http"
storage_url = f"{protocol}://storage_1:8000"
```

**Archivo:** `app/client/app.py`

```python
# ANTES
api_url = os.getenv("API_BASE_URL", "http://processor_1:8000")

# DESPUÉS
use_https = os.getenv("USE_HTTPS", "false").lower() == "true"
protocol = "https" if use_https else "http"
api_url = os.getenv("API_BASE_URL", f"{protocol}://processor_1:8000")

# Deshabilitar verificación SSL para certificados auto-firmados
import urllib3
if use_https:
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    requests.packages.urllib3.disable_warnings()
```

### Paso 6: Verificación

```bash
# Verificar que certificados fueron creados
ls -la certs/

# Construir imágenes
docker build -t file-search-processor:latest -f Dockerfile.processor .

# Correr con HTTPS habilitado
docker run -e USE_HTTPS=true \
  -v $(pwd)/certs:/app/certs:ro \
  -p 8000:8000 \
  file-search-processor:latest

# Verificar HTTPS (con certificado auto-firmado)
curl --insecure https://localhost:8000/health
# Respuesta: 200 OK
```

---

## 🎯 Resumen de Implementación

| Solución | Tiempo | Dificultad | Prioridad | Impacto |
|----------|--------|-----------|-----------|---------|
| 1. Sanitizar Logs | 5-10 min | ⭐ | 🟡 Media | 🟡 Medio |
| 2. CORS Restrictivo | 5-8 min | ⭐ | 🔴 Crítico | 🔴 Crítico |
| 3. Sin Root | 8-12 min | ⭐ | 🔴 Crítico | 🟠 Alto |
| 4. Path Traversal | 10-15 min | ⭐ | 🔴 Crítico | 🔴 Crítico |
| 5. Límites Entrada | 12-18 min | ⭐ | 🟡 Media | 🟡 Medio |
| 6. Secrets Mgmt | 15-20 min | ⭐⭐ | 🟠 Alto | 🟠 Alto |
| 7. Rate Limiting | 18-25 min | ⭐⭐ | 🟡 Media | 🟡 Medio |
| 8. Autenticación | 25-35 min | ⭐⭐ | 🔴 Crítico | 🔴 Crítico |
| 9. HTTPS/TLS | 45-60 min | ⭐⭐⭐ | 🔴 Crítico | 🔴 Crítico |

---

## 📋 Plan de Implementación Recomendado

### **Semana 1 (Crítico - 2-3 horas):**
1. ✅ CORS Restrictivo (5-8 min)
2. ✅ Path Traversal (10-15 min)
3. ✅ Correr sin Root (8-12 min)
4. ✅ Sanitizar Logs (5-10 min)

### **Semana 2 (Importante - 1.5-2 horas):**
5. ✅ Autenticación con Tokens (25-35 min)
6. ✅ Secrets Management (15-20 min)
7. ✅ Rate Limiting (18-25 min)

### **Semana 3 (Robustez - 1-1.5 horas):**
8. ✅ Limitaciones de Entrada (12-18 min)
9. ✅ HTTPS/TLS (45-60 min)

---

## ✅ Checklist de Verificación

- [ ] CORS restringido a orígenes específicos
- [ ] Path traversal validado en download
- [ ] Contenedores corren como usuario no-root
- [ ] Logs no contienen información sensible
- [ ] Todos los endpoints requieren autenticación
- [ ] Variables de entorno configuradas desde .env
- [ ] Rate limiting implementado (30/min para searches)
- [ ] Input validado con max_length y máximos
- [ ] HTTPS/TLS habilitado en producción

---

**Fecha de Creación:** 7 de enero de 2026  
**Última Actualización:** 7 de enero de 2026  
**Estado:** Listo para implementar
