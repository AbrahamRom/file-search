# File Search

Plataforma compuesta por un backend FastAPI y un cliente web en Streamlit para localizar, filtrar y descargar archivos alojados en un volumen compartido.

## 🎯 Características principales

- Escaneo recurrente del directorio configurado (por defecto `/app/files`) con sincronización automática en SQLite.
- API REST con endpoints de salud, búsqueda paginada, consulta y descarga de archivos.
- Cliente Streamlit con búsqueda por nombre, filtro por tipo, paginación y enlaces de descarga.
- Sistema de logging unificado (aplicación y access logs) escribiendo en `/app/logs`.
- **Arquitectura DNS de Alta Disponibilidad** con servidor primario, dos backups y proxy con failover automático.
- Imágenes Docker separadas para la API (`Dockerfile.server`) y el cliente (`Dockerfile.client`) listas para ejecutarse en redes Swarm o entornos distribuidos.

## 🌐 Arquitectura DNS de Alta Disponibilidad

El sistema incluye una arquitectura robusta de DNS con alta disponibilidad:

### Componentes

1. **DNS Proxy** (`dns_proxy` - Puerto 5350)
   - Punto de entrada único para todas las solicitudes DNS
   - Failover automático entre servidores
   - Health checks periódicos de todos los servidores DNS
   - Enrutamiento inteligente hacia servidores saludables

2. **DNS Primario** (`dns_primary` - Puerto 5353)
   - Servidor DNS principal que maneja resoluciones
   - Propaga actualizaciones a servidores backup
   - Cache de resoluciones DNS
   - Logging completo de todas las operaciones

3. **DNS Backup 1** (`dns_backup_1` - Puerto 5354)
   - Servidor de respaldo que sincroniza con el primario
   - Sincronización periódica cada 30 segundos
   - Toma el control automáticamente si el primario falla
   - Mantiene cache sincronizado

4. **DNS Backup 2** (`dns_backup_2` - Puerto 5355)
   - Segundo servidor de respaldo independiente
   - Proporciona redundancia adicional
   - Sincronización automática con el primario
   - Failover de tercer nivel

### Características de Alta Disponibilidad

- **Replicación automática**: Los servidores backup se sincronizan automáticamente con el primario cada 30 segundos
- **Failover transparente**: El proxy detecta fallos y redirige el tráfico sin intervención manual
- **Health monitoring**: Verificación continua del estado de todos los servidores cada 10 segundos
- **Cache distribuido**: Cada servidor mantiene su propio cache para respuestas rápidas
- **Logging exhaustivo**: Todas las operaciones se registran con identificadores de servidor
- **Recuperación automática**: Servidores caídos se reintegran automáticamente al recuperarse

### Flujo de Operación

1. Cliente solicita resolución DNS al proxy (puerto 5350)
2. Proxy intenta resolver con el servidor primario
3. Si el primario falla, proxy intenta con backup_1
4. Si backup_1 falla, proxy intenta con backup_2
5. Servidores backup sincronizan su cache con el primario cada 30 segundos
6. Health checks actualizan el estado de disponibilidad cada 10 segundos

## 📂 Estructura relevante

```text
app/
├── main.py              # Punto de entrada (configura logs y expone la app FastAPI)
├── dns_service/
│   ├── main.py          # Servidor DNS con sincronización y cache
│   ├── proxy.py         # Proxy DNS con failover automático
│   ├── logging_config.py# Configuración de logs DNS
│   ├── Dockerfile       # Imagen para servidores DNS
│   └── Dockerfile.proxy # Imagen para proxy DNS
├── server/
│   ├── api/endpoints.py # Endpoints de la API
│   ├── logging_config.py# Configuración centralizada de logging
│   ├── db/
│   │   ├── db.py        # Inicialización de SQLite
│   │   └── crud.py      # Operaciones sobre la tabla files
│   └── services/
│       ├── scanner.py   # Escaneo de archivos y sincronización
│       └── file_handler.py
├── client/
│   ├── app.py           # Interfaz Streamlit
│   └── ui_components.py # Componentes reutilizables de UI
├── files/               # Ejemplos para pruebas locales (monta tu propio volumen en producción)
└── logs/                # Carpeta objetivo para logs (se recomienda montarla como volumen)
```

## 🚀 Puesta en marcha rápida (Docker)

docker build -f Dockerfile.server -t file-search-api .

docker run --rm -p 8000:8000 -v "${PWD}/runtime/files:/app/files" -v "${PWD}/runtime/logs:/app/logs" file-search-api

docker build -f Dockerfile.client -t file-search-client .

docker run --rm -p 8501:8501 -e API_BASE_URL=http://host.docker.internal:8000 file-search-client

### Puesta en marcha con Docker Compose

La arquitectura de alta disponibilidad DNS se despliega automáticamente con Docker Compose:

```bash
# 1. Elige la carpeta de archivos que deseas compartir (por ejemplo /home/usuario/documentos)
export FILES_SOURCE=/home/usuario/documentos

# 2. (Opcional) Cambia la ruta interna del contenedor donde se indexarán los archivos
export FILES_ROOT=/app/files

# 3. Inicia todos los servicios (incluye DNS Proxy + Primary + 2 Backups)
docker compose up --build
```

Los siguientes servicios estarán disponibles:

- **DNS Proxy**: `http://localhost:5350` - Punto de entrada con failover
- **DNS Primario**: `http://localhost:5353` - Servidor principal
- **DNS Backup 1**: `http://localhost:5354` - Primera réplica
- **DNS Backup 2**: `http://localhost:5355` - Segunda réplica
- **API**: `http://localhost:8000` - API de archivos
- **Cliente Web**: `http://localhost:8501` - Interfaz Streamlit

### Verificar estado de DNS

```bash
# Estado del proxy y servidores
curl http://localhost:5350/health

# Estado del servidor primario
curl http://localhost:5353/health

# Estado de los backups
curl http://localhost:5354/health
curl http://localhost:5355/health
```

### Probar resolución DNS

```bash
# Resolver un hostname a través del proxy
curl http://localhost:5350/resolve/server

# Resolver directamente desde el primario
curl http://localhost:5353/resolve/client
```

---

## Funcionalidad de Archivos

El proyecto incluye una funcionalidad para usar una carpeta de archivos como base de datos:

- La carpeta `files` se utiliza para almacenar los archivos subidos
- Los archivos se cargan automáticamente y se registran en la base de datos
- Se puede acceder a los archivos a través de la API

### Cómo subir archivos

Para subir un archivo, utiliza el endpoint `/upload`:

```
POST /upload
```

Parámetros:
- `file`: El archivo a subir (multipart/form-data)
- `folder`: (Opcional) Subcarpeta donde guardar el archivo

Ejemplo de respuesta:
```json
{
  "status": "success",
  "file_id": "7f8e9d1c2b3a4f5e6d7c8b9a",
  "filename": "documento.pdf",
  "size": 12345
}
```

Los archivos subidos estarán disponibles para búsqueda y descarga a través de los endpoints existentes.

```bash
# 1. Elige la carpeta de archivos que deseas compartir (por ejemplo D:\MisDocumentos)
$env:FILES_SOURCE = "D:\Library"

# 2. (Opcional) Cambia la ruta interna del contenedor donde se indexarán los archivos
$env:FILES_ROOT = "/app/files"

# 3. Inicia ambos servicios
docker compose up --build
```

Por defecto, si no defines `FILES_SOURCE`, se usará `./runtime/files`.

- La API estará disponible en `http://localhost:8000` (documentación en `/docs`).
- El cliente Streamlit estará en `http://localhost:8501`.
- Puedes ajustar `API_BASE_URL` en el cliente si la API está en otra dirección.

Ejemplo para cambiar la carpeta de archivos:

```bash
FILES_SOURCE=/home/usuario/documentos docker compose up --build
```

Esto montará `/home/usuario/documentos` en el contenedor y la API indexará esos archivos.

## 🧾 Endpoints principales

| Método | Ruta                         | Descripción                               |
|--------|------------------------------|-------------------------------------------|
| GET    | `/health`                    | Verifica el estado del servicio.          |
| GET    | `/search?query&limit&offset` | Búsqueda paginada de archivos.            |
| GET    | `/files/{file_id}`           | Obtiene metadatos completos del archivo.  |
| GET    | `/files/{file_id}/download`  | Descarga el archivo.                      |
| POST   | `/files`                     | Registra o actualiza un archivo.          |
| DELETE | `/files/{file_id}`           | Elimina un registro existente.            |

La documentación automática de FastAPI está disponible en `http://API_HOST:8000/docs`.

## 🗃️ Logging

- Los logs se guardan en `/app/logs` (`application.log` y `access.log`).
- Cambia el destino o niveles con `LOG_DIR`, `LOG_LEVEL` y `ACCESS_LOG_LEVEL`.
- Monta un volumen en `/app/logs` para persistirlos en producción.

## Desarrollo local (opcional)

```bash
pip install -r app/server/requirements.txt
FILES_ROOT=./app/files bash app/start.sh
```

El directorio `app/files` del repositorio contiene ejemplos para pruebas locales rápidas; en entornos reales apunta `FILES_ROOT` a la ruta que quieras indexar.

## 🧪 Pruebas rápidas

```bash
PYTHONPATH=$(pwd) python3 -m pytest tests
```

Si aún no cuentas con tests implementados, puedes usar el `TestClient` de FastAPI para validar manualmente:

```bash
PYTHONPATH=$(pwd) python3 -c "from fastapi.testclient import TestClient; from app.server.api.endpoints import app; client = TestClient(app); print(client.get('/health').json())"
```

## 📌 Notas adicionales

- Al construir la imagen del servidor no se empaquetan archivos de datos; monta la carpeta deseada en `/app/files` en runtime.
- Para despliegues en Swarm o Kubernetes declara volúmenes/claims para `/app/files` y `/app/logs`, y expone la variable `API_BASE_URL` en el cliente para alcanzar la API.
- Ajusta `DB_PATH` si quieres que la base SQLite viva fuera del contenedor.

---
