FROM python:3.11-slim

# Evitar prompts interactivos y reducir tamaño
ENV DEBIAN_FRONTEND=noninteractive
WORKDIR /app

# Copiar requirements y instalar dependencias
COPY app/server/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copiar el resto de la aplicación
# - mantenemos el layout local:
#   /app/main.py           <- entrypoint
#   /app/server/...        <- código del servidor (incluye api/)
COPY app/ /app/

# Declarar ARG para evitar advertencias de linters sobre variables no definidas
ARG PYTHONPATH=""

# Asegurar que Python pueda resolver "import api.*" que está en app/server/api
ENV PYTHONPATH=/app/server:$PYTHONPATH

EXPOSE 8000

# Ejecutar el main que invoca uvicorn
CMD ["python", "/app/main.py"]