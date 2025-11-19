FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Instalar dependencias
COPY app/dns_service/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt && rm /tmp/requirements.txt

# Copiar estructura de paquetes necesaria
# Necesitamos __init__.py para que app sea un paquete
COPY app/__init__.py /app/__init__.py
COPY app/dns_service /app/dns_service

# Crear directorio de logs
RUN mkdir -p /app/logs
ENV LOG_DIR=/app/logs
ENV PYTHONPATH=/app

# Puerto solicitado
EXPOSE 5353

# Ejecutar uvicorn apuntando al módulo dentro del paquete app
CMD ["uvicorn", "app.dns_service.main:app", "--host", "0.0.0.0", "--port", "5353"]