FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1

# Cambiamos el WORKDIR a la raíz del sistema de archivos para que /app sea un módulo importable
WORKDIR /

COPY app/dns_service/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --default-timeout=1000 -r /tmp/requirements.txt && rm /tmp/requirements.txt

# Copiamos la estructura manteniendo la carpeta 'app'
COPY app/__init__.py /app/__init__.py
COPY app/dns_service /app/dns_service

RUN mkdir -p /app/logs
ENV LOG_DIR=/app/logs
# Añadimos / (root) al PYTHONPATH para que 'import app.dns_service' funcione
ENV PYTHONPATH=/

EXPOSE 5353

CMD ["uvicorn", "app.dns_service.main:app", "--host", "0.0.0.0", "--port", "5353"]