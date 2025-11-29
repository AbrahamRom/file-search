# File Search

Plataforma compuesta por un backend FastAPI y un cliente web en Streamlit para localizar, filtrar y descargar archivos alojados en un volumen compartido.

## 🎯 Características principales

- Escaneo recurrente del directorio configurado (por defecto `/app/files`) con sincronización automática en SQLite.
- API REST con endpoints de salud, búsqueda paginada, consulta y descarga de archivos.
- Cliente Streamlit con búsqueda por nombre, filtro por tipo, paginación y enlaces de descarga.
- Sistema de logging unificado (aplicación y access logs) escribiendo en `/app/logs`.
- Imágenes Docker separadas para la API (`Dockerfile.server`) y el cliente (`Dockerfile.client`) listas para ejecutarse en redes Swarm o entornos distribuidos.

## 📂 Estructura relevante

```text
app/
├── main.py              # Punto de entrada (configura logs y expone la app FastAPI)
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

Puedes inicializar ambos servicios y definir la ruta de los archivos a indexar usando variables de entorno:

```bash
# 1. Elige la carpeta de archivos que deseas compartir (por ejemplo /home/usuario/documentos)
export FILES_SOURCE=/home/usuario/documentos

# 2. (Opcional) Cambia la ruta interna del contenedor donde se indexarán los archivos
export FILES_ROOT=/app/files

# 3. Inicia ambos servicios
docker compose up --build
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

## 🔁 Failover del Cliente (Streamlit) sin cambiar URL

Este proyecto implementa alta disponibilidad del cliente usando un router de failover basado en HAProxy:

- Dos instancias del cliente (`client_primary` y `client_backup`) corren dentro de la red interna de Docker, sin exponer puertos al host.
- Un `client_router` expone el puerto `8501` al host y realiza health checks contra ambas instancias.
- Si el cliente primario cae, el router conmuta automáticamente al respaldo manteniendo la misma URL: `http://localhost:8501`.

Cómo arrancar:

```bash
docker compose up --build -d
```

Verifica el estado:

- Cliente (router): `http://localhost:8501`
- API: `http://localhost:8000/health`
- DNS interno: `http://localhost:5353/health`

Simular caída del cliente primario y ver conmutación:

```bash
# Parar el primario
docker compose stop client_primary

# El router seguirá sirviendo en 8501 usando el respaldo
# Para restaurar primario
docker compose start client_primary
```

Notas:

- No se han añadido nuevos volúmenes; se reutilizan los existentes para archivos y logs.
- El `client_router` utiliza health checks HTTP simples (`GET /`) compatibles con Streamlit.
- La resolución DNS interna (servicio `dns_service`) se mantiene para el descubrimiento de la API por parte del cliente.

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
