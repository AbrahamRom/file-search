# File Search

Plataforma compuesta por un backend FastAPI y un cliente web en Streamlit para localizar, filtrar y descargar archivos dentro de un directorio montado en el contenedor.

## 🎯 Características principales

- Escaneo recurrente de `app/files/` con sincronización automática en SQLite.
- API REST con endpoints de salud, búsqueda paginada, consulta y descarga.
- Cliente Streamlit con búsqueda por nombre, filtro por tipo de archivo, paginación y enlaces de descarga.
- Sistema de logging unificado (aplicación y access logs) escribiendo en `app/logs/`.
- Imagen Docker lista para levantar API y cliente en un solo contenedor.

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
├── files/               # Archivos de ejemplo (montados en runtime)
└── logs/                # Logs de aplicación y access (persisten en app/logs)
```

## 🚀 Puesta en marcha rápida (Docker)

El contenedor se encarga de instalar dependencias, inicializar la base de datos, escanear los archivos y lanzar tanto la API (FastAPI) como la UI (Streamlit).

```bash
# 1. Clona el repositorio y accede a la carpeta raíz
git clone <repo>
cd file-search

# 2. Construye la imagen
docker build -t file-search .

# 3. Ejecuta el contenedor (puertos 8000 y 8501)
docker run --rm \
  -p 8000:8000 \
  -p 8501:8501 \
  -v $(pwd)/app/logs:/app/logs \
  file-search
```

- API disponible en `http://localhost:8000` (documentación en `/docs`).
- UI disponible en `http://localhost:8501`.
- Los volúmenes son opcionales pero recomendados para persistir los archivos a escanear (`app/files`) y los registros (`app/logs`).

> Si solo quieres probar rápidamente, puedes omitir los volúmenes; el contenedor usará los archivos de ejemplo incluidos.

## 🧾 Endpoints principales

| Método | Ruta                         | Descripción                               |
|--------|------------------------------|-------------------------------------------|
| GET    | `/health`                    | Verifica el estado del servicio.          |
| GET    | `/search?query&limit&offset` | Búsqueda paginada de archivos.            |
| GET    | `/files/{file_id}`           | Obtiene metadatos completos del archivo.  |
| GET    | `/files/{file_id}/download`  | Descarga el archivo.                      |
| POST   | `/files`                     | Registra o actualiza un archivo.          |
| DELETE | `/files/{file_id}`           | Elimina un registro existente.            |

La documentación automática de FastAPI está disponible en `http://localhost:8000/docs`.

## 🗃️ Logging

- Los logs se guardan en `app/logs/` (archivos `application.log` y `access.log`).
- Puedes cambiar el directorio y nivel de log con las variables de entorno `LOG_DIR`, `LOG_LEVEL` y `ACCESS_LOG_LEVEL`.
- Recuerda mapear `./app/logs:/app/logs` al ejecutar en Docker para persistir los registros.

## Desarrollo local (opcional)

Si prefieres ejecutar la aplicación sin Docker (p. ej. para depuración rápida), bastará con instalar las dependencias y lanzar `app/start.sh`, que replica el comportamiento del contenedor:

```bash
pip install -r app/server/requirements.txt
FILES_ROOT=./app/files bash app/start.sh
```

> No es necesario crear entornos virtuales si vas a trabajar exclusivamente dentro del contenedor.

## 🧪 Pruebas rápidas

```bash
PYTHONPATH=$(pwd) python3 -m pytest tests
```

Si aún no cuentas con tests implementados, puedes usar el `TestClient` de FastAPI para validar manualmente:

```bash
PYTHONPATH=$(pwd) python3 -c "from fastapi.testclient import TestClient; from app.server.api.endpoints import app; client = TestClient(app); print(client.get('/health').json())"
```

## 📌 Notas adicionales

- Personaliza la carpeta `app/files/` con tus documentos. Al reconstruir la imagen, puedes copiar archivos de ejemplo o montarlos como volumen en runtime.
- El escaneo utiliza la ruta `FILES_ROOT` (por defecto `app/files/`). Define esta variable si deseas apuntar a otra carpeta dentro del contenedor.

---
