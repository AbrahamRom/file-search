FROM python:3.11-slim

WORKDIR /app

# Instalar dependencias
COPY app/server/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY app/server/ /app/server/