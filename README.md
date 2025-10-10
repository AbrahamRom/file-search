# File Search API

API REST construida con **FastAPI** que permite registrar, buscar y eliminar información de archivos escaneados en un directorio local. Está pensada para actuar como backend de una herramienta de exploración y búsqueda de documentos.

## 🎯 Características principales

- Endpoint `/health` para comprobar el estado del servicio.
- Registro y actualización de metadatos de archivos con `/files` (POST).
- Eliminación de archivos mediante `/files/{file_id}` (DELETE).
- Búsqueda paginada con filtros usando `/search`.
- Persistencia basada en SQLite (el archivo se crea automáticamente al primer arranque).
- Imagen Docker lista para producción.

## 📂 Estructura relevante

```text
app/
├── main.py            # Punto de entrada de la API (uvicorn)
├── server/
│   ├── api/endpoints.py   # Definición de endpoints FastAPI
│   ├── db/
│   │   ├── db.py          # Inicialización de la base de datos
│   │   └── crud.py        # Operaciones CRUD
│   └── services/
│       ├── scanner.py     # Lógica de escaneo de archivos
│       └── file_handler.py
├── client/             # UI (Streamlit)
└── files/              # Archivos de ejemplo
```

## 🐳 Ejecutar con Docker

```bash
# Construir la imagen (desde la raíz del repo)
docker build -t file-search-api .

# Ejecutar la imagen
docker run --rm -p 8000:8000 file-search-api
```

La API quedará disponible en `http://localhost:8000/health`. La documentación automática de FastAPI está en `http://localhost:8000/docs`.

### Variables de entorno

> La base de datos SQLite se almacenará dentro del contenedor en `/app/server`.

## ✅ Endpoints

| Método | Ruta              | Descripción                                   |
|--------|-------------------|-----------------------------------------------|
| GET    | `/health`         | Verifica el estado del servicio.             |
| POST   | `/files`          | Registra o actualiza un archivo.             |
| DELETE | `/files/{file_id}`| Elimina un archivo registrado.               |
| GET    | `/search`         | Busca archivos por texto (query, limit, offset). |

## 📌 Notas adicionales

- Consulta la carpeta `app/files/` para ejemplos de documentos que puedes indexar.

---
