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

### Backend (FastAPI)

```bash
# 1. Construye la imagen del servidor
docker build -f Dockerfile.server -t file-search-api .

# 2. Arranca la API montando los directorios que quieras compartir/persistir
mkdir -p runtime/files runtime/logs

docker run --rm -p 8000:8000 -v "${PWD}/runtime/files:/app/files" -v "${PWD}/runtime/logs:/app/logs" file-search-api
```

- API disponible en `http://localhost:8000` (documentación en `/docs`).
- Si ya tienes los archivos en otra ruta, reemplaza `$(pwd)/runtime/files` por la carpeta que desees compartir.
- Puedes ajustar los puertos o añadir variables de entorno (`FILES_ROOT`, `LOG_DIR`, `DB_PATH`, etc.) según tus necesidades.

### Cliente (Streamlit)

```bash
# 1. Construye la imagen del cliente
docker build -f Dockerfile.client -t file-search-client .

# 2. Arranca la UI apuntando a la URL de la API
docker run --rm -p 8501:8501 -e API_BASE_URL=http://host.docker.internal:8000 file-search-client
```

- Ajusta `API_BASE_URL` para que apunte al servicio FastAPI accesible desde el contenedor (por ejemplo `http://file-search-api:8000` en Swarm o Compose).
- La interfaz estará disponible en `http://localhost:8501`.

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
