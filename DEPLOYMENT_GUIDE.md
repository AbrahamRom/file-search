# Guía de Despliegue - Sistema de Búsqueda de Archivos con DNS HA

Este documento describe tres métodos para desplegar el sistema distribuido de búsqueda de archivos con DNS de alta disponibilidad.

---

## Índice

1. [Requisitos Previos](#requisitos-previos)
2. [Método 1: Docker Compose (Desarrollo Local)](#método-1-docker-compose-desarrollo-local)
3. [Método 2: Docker Swarm con Stack (Producción)](#método-2-docker-swarm-con-stack-producción)
4. [Método 3: Docker Run Manual (Comando a Comando)](#método-3-docker-run-manual-comando-a-comando)
5. [Verificación del Sistema](#verificación-del-sistema)
6. [Troubleshooting](#troubleshooting)

---

## Requisitos Previos

### Software necesario
- Docker Engine 20.10+
- Docker Compose v2+
- (Para Swarm) 2+ máquinas con Docker instalado y conectividad de red

### Estructura de directorios
```bash
# Crear directorios necesarios
mkdir -p ./runtime/logs ./runtime/files

# Para Swarm (en cada nodo)
sudo mkdir -p /srv/file-search/logs /srv/file-search/files
sudo chmod 777 /srv/file-search/logs /srv/file-search/files
```

### Construir imágenes
```bash
# Desde el directorio raíz del proyecto
docker build -t file-search-dns:latest -f app/dns_service/Dockerfile .
docker build -t file-search-api:latest -f Dockerfile.server .
docker build -t file-search-client:latest -f Dockerfile.client .
```

---

## Método 1: Docker Compose (Desarrollo Local)

Este método es ideal para **desarrollo local** y pruebas en una sola máquina.

### Despliegue completo

```bash
# Levantar todos los servicios
docker compose up -d

# Ver logs en tiempo real
docker compose logs -f

# Ver estado de los servicios
docker compose ps
```

### Despliegue por etapas

```bash
# 1. Levantar primero los servidores DNS
docker compose up -d dns_1 dns_2 dns_3

# 2. Esperar a que estén healthy (aprox. 15 segundos)
sleep 15
docker compose ps

# 3. Levantar el servidor API
docker compose up -d server

# 4. Levantar el cliente web
docker compose up -d client
```

### Verificar el clúster DNS

```bash
# Ver estado del clúster desde cualquier DNS
curl -s http://localhost:5353/cluster-status | python3 -m json.tool

# Ver lista de servidores DNS disponibles
curl -s http://localhost:5353/dns-servers | python3 -m json.tool
```

### Comandos útiles

```bash
# Detener todo
docker compose down

# Reiniciar un servicio específico
docker compose restart dns_1

# Ver logs de un servicio
docker compose logs -f dns_1

# Escalar (no recomendado, usar servicios separados)
# docker compose up -d --scale dns_1=1
```

### Puertos expuestos

| Servicio | Puerto Local | Descripción |
|----------|--------------|-------------|
| dns_1    | 5353         | Servidor DNS 1 |
| dns_2    | 5354         | Servidor DNS 2 |
| dns_3    | 5355         | Servidor DNS 3 |
| server   | 8000         | API REST |
| client   | 8501         | Interfaz Web (Streamlit) |

---

## Método 2: Docker Swarm con Stack (Producción)

Este método es ideal para **producción** con múltiples nodos físicos.

### Preparación del Swarm (2 computadoras)

#### En la máquina MANAGER (PC1)

```bash
# 1. Obtener la IP de esta máquina
ip addr show | grep "inet " | grep -v 127.0.0.1

# 2. Inicializar el Swarm (reemplazar <IP_MANAGER> con tu IP)
docker swarm init --advertise-addr <IP_MANAGER>

# Ejemplo:
docker swarm init --advertise-addr 192.168.1.100

# 3. Guardar el token que aparece para unir workers
# Se verá algo como:
# docker swarm join --token SWMTKN-1-xxx 192.168.1.100:2377
```

#### En la máquina WORKER (PC2)

```bash
# Unirse al swarm usando el comando del paso anterior
docker swarm join --token SWMTKN-1-xxx 192.168.1.100:2377
```

#### Verificar el Swarm (en Manager)

```bash
# Ver nodos del swarm
docker node ls

# Debería mostrar algo como:
# ID                           HOSTNAME   STATUS    AVAILABILITY   MANAGER STATUS
# abc123 *                     pc1        Ready     Active         Leader
# def456                       pc2        Ready     Active
```

### Distribuir imágenes a los nodos

**Opción A: Registry local**
```bash
# En el manager, crear registry local
docker run -d -p 5000:5000 --restart=always --name registry registry:2

# Tagear y subir imágenes
docker tag file-search-dns:latest localhost:5000/file-search-dns:latest
docker tag file-search-api:latest localhost:5000/file-search-api:latest
docker tag file-search-client:latest localhost:5000/file-search-client:latest

docker push localhost:5000/file-search-dns:latest
docker push localhost:5000/file-search-api:latest
docker push localhost:5000/file-search-client:latest

# Actualizar stack.yml para usar localhost:5000/file-search-*:latest
```

**Opción B: Guardar y cargar imágenes manualmente**
```bash
# En el manager, guardar imágenes
docker save file-search-dns:latest | gzip > file-search-dns.tar.gz
docker save file-search-api:latest | gzip > file-search-api.tar.gz
docker save file-search-client:latest | gzip > file-search-client.tar.gz

# Copiar a worker (usando scp, usb, etc.)
scp file-search-*.tar.gz usuario@<IP_WORKER>:/tmp/

# En el worker, cargar imágenes
gunzip -c /tmp/file-search-dns.tar.gz | docker load
gunzip -c /tmp/file-search-api.tar.gz | docker load
gunzip -c /tmp/file-search-client.tar.gz | docker load
```

### Desplegar el Stack

```bash
# Desde el manager, desplegar el stack
docker stack deploy -c stack.yml file-search

# Ver servicios desplegados
docker stack services file-search

# Ver tareas (contenedores) del stack
docker stack ps file-search
```

### Verificar distribución

```bash
# Ver en qué nodos están corriendo los servicios
docker service ps file-search_dns_1
docker service ps file-search_dns_2
docker service ps file-search_dns_3
docker service ps file-search_server
docker service ps file-search_client

# Ejemplo de salida:
# ID             NAME                   NODE      CURRENT STATE
# abc123         file-search_dns_1.1    pc1       Running
# def456         file-search_dns_2.1    pc2       Running
# ghi789         file-search_dns_3.1    pc2       Running
```

### Comandos útiles para Swarm

```bash
# Ver logs de un servicio
docker service logs -f file-search_dns_1

# Escalar un servicio
docker service scale file-search_dns_1=2

# Actualizar un servicio
docker service update --image file-search-dns:v2 file-search_dns_1

# Eliminar el stack completo
docker stack rm file-search

# Ver redes del swarm
docker network ls | grep file-search
```

---

## Método 3: Docker Run Manual (Comando a Comando)

Este método es útil para **entender el sistema** o cuando no se puede usar Compose/Swarm.

### Preparación de la red

```bash
# Crear red bridge para comunicación entre contenedores
docker network create --driver bridge file-search-net
```

### En un escenario con Docker Swarm (2 PCs)

```bash
# Crear red overlay (ejecutar en el manager)
docker network create --driver overlay --attachable file-search-net
```

### Paso 1: Levantar Servidor DNS 1 (PC1 - Manager)

```bash
docker run -d \
  --name dns_1 \
  --hostname dns_1 \
  --network file-search-net \
  --network-alias dns \
  -p 5353:5353 \
  -v $(pwd)/runtime/logs:/app/logs \
  -e LOG_DIR=/app/logs \
  -e LOG_LEVEL=INFO \
  -e DNS_SERVER_ID=dns_1 \
  -e DNS_PORT=5353 \
  -e DNS_ALIAS=dns \
  -e HEALTH_CHECK_INTERVAL=5 \
  -e SYNC_INTERVAL=30 \
  -e DISCOVERY_INTERVAL=15 \
  --restart unless-stopped \
  file-search-dns:latest
```

### Paso 2: Levantar Servidor DNS 2 (PC2 - Worker)

```bash
docker run -d \
  --name dns_2 \
  --hostname dns_2 \
  --network file-search-net \
  --network-alias dns \
  -p 5354:5353 \
  -v /srv/file-search/logs:/app/logs \
  -e LOG_DIR=/app/logs \
  -e LOG_LEVEL=INFO \
  -e DNS_SERVER_ID=dns_2 \
  -e DNS_PORT=5353 \
  -e DNS_ALIAS=dns \
  -e HEALTH_CHECK_INTERVAL=5 \
  -e SYNC_INTERVAL=30 \
  -e DISCOVERY_INTERVAL=15 \
  --restart unless-stopped \
  file-search-dns:latest
```

### Paso 3: Levantar Servidor DNS 3 (PC2 - Worker)

```bash
docker run -d \
  --name dns_3 \
  --hostname dns_3 \
  --network file-search-net \
  --network-alias dns \
  -p 5355:5353 \
  -v /srv/file-search/logs:/app/logs \
  -e LOG_DIR=/app/logs \
  -e LOG_LEVEL=INFO \
  -e DNS_SERVER_ID=dns_3 \
  -e DNS_PORT=5353 \
  -e DNS_ALIAS=dns \
  -e HEALTH_CHECK_INTERVAL=5 \
  -e SYNC_INTERVAL=30 \
  -e DISCOVERY_INTERVAL=15 \
  --restart unless-stopped \
  file-search-dns:latest
```

### Paso 4: Esperar descubrimiento del clúster DNS

```bash
# Esperar 20 segundos para que los DNS se descubran entre sí
sleep 20

# Verificar el clúster
curl -s http://localhost:5353/cluster-status | python3 -m json.tool
```

### Paso 5: Levantar Servidor API (PC1 - Manager)

```bash
docker run -d \
  --name server \
  --hostname server \
  --network file-search-net \
  -p 8000:8000 \
  -v $(pwd)/runtime/files:/app/files \
  -v $(pwd)/runtime/logs:/app/logs \
  -e FILES_ROOT=/app/files \
  -e LOG_DIR=/app/logs \
  -e DNS_ALIAS=dns \
  -e DNS_SERVICE_PORT=5353 \
  --restart unless-stopped \
  file-search-api:latest
```

### Paso 6: Levantar Cliente Web (PC2 - Worker)

```bash
docker run -d \
  --name client \
  --hostname client \
  --network file-search-net \
  -p 8501:8501 \
  -e DNS_ALIAS=dns \
  -e DNS_SERVICE_PORT=5353 \
  -e TARGET_SERVICE_NAME=server \
  -e TARGET_SERVICE_PORT=8000 \
  -e API_BASE_URL=http://server:8000 \
  -e BROWSER_API_URL=http://<IP_MANAGER>:8000 \
  --restart unless-stopped \
  file-search-client:latest
```

> **Nota:** Reemplazar `<IP_MANAGER>` con la IP real de la máquina manager.

### Script completo para PC1 (Manager)

```bash
#!/bin/bash
# deploy-manager.sh

set -e

echo "=== Creando red ==="
docker network create --driver overlay --attachable file-search-net 2>/dev/null || true

echo "=== Levantando DNS 1 ==="
docker run -d \
  --name dns_1 \
  --hostname dns_1 \
  --network file-search-net \
  --network-alias dns \
  -p 5353:5353 \
  -v /srv/file-search/logs:/app/logs \
  -e LOG_DIR=/app/logs \
  -e LOG_LEVEL=INFO \
  -e DNS_SERVER_ID=dns_1 \
  -e DNS_PORT=5353 \
  -e DNS_ALIAS=dns \
  -e HEALTH_CHECK_INTERVAL=5 \
  -e SYNC_INTERVAL=30 \
  -e DISCOVERY_INTERVAL=15 \
  --restart unless-stopped \
  file-search-dns:latest

echo "=== Esperando 10 segundos ==="
sleep 10

echo "=== Levantando Server API ==="
docker run -d \
  --name server \
  --hostname server \
  --network file-search-net \
  -p 8000:8000 \
  -v /srv/file-search/files:/app/files \
  -v /srv/file-search/logs:/app/logs \
  -e FILES_ROOT=/app/files \
  -e LOG_DIR=/app/logs \
  -e DNS_ALIAS=dns \
  -e DNS_SERVICE_PORT=5353 \
  --restart unless-stopped \
  file-search-api:latest

echo "=== Despliegue en Manager completado ==="
```

### Script completo para PC2 (Worker)

```bash
#!/bin/bash
# deploy-worker.sh

set -e

MANAGER_IP="${1:-192.168.1.100}"  # Pasar IP del manager como argumento

echo "=== Levantando DNS 2 ==="
docker run -d \
  --name dns_2 \
  --hostname dns_2 \
  --network file-search-net \
  --network-alias dns \
  -p 5354:5353 \
  -v /srv/file-search/logs:/app/logs \
  -e LOG_DIR=/app/logs \
  -e LOG_LEVEL=INFO \
  -e DNS_SERVER_ID=dns_2 \
  -e DNS_PORT=5353 \
  -e DNS_ALIAS=dns \
  -e HEALTH_CHECK_INTERVAL=5 \
  -e SYNC_INTERVAL=30 \
  -e DISCOVERY_INTERVAL=15 \
  --restart unless-stopped \
  file-search-dns:latest

echo "=== Levantando DNS 3 ==="
docker run -d \
  --name dns_3 \
  --hostname dns_3 \
  --network file-search-net \
  --network-alias dns \
  -p 5355:5353 \
  -v /srv/file-search/logs:/app/logs \
  -e LOG_DIR=/app/logs \
  -e LOG_LEVEL=INFO \
  -e DNS_SERVER_ID=dns_3 \
  -e DNS_PORT=5353 \
  -e DNS_ALIAS=dns \
  -e HEALTH_CHECK_INTERVAL=5 \
  -e SYNC_INTERVAL=30 \
  -e DISCOVERY_INTERVAL=15 \
  --restart unless-stopped \
  file-search-dns:latest

echo "=== Esperando 15 segundos para descubrimiento ==="
sleep 15

echo "=== Levantando Cliente Web ==="
docker run -d \
  --name client \
  --hostname client \
  --network file-search-net \
  -p 8501:8501 \
  -e DNS_ALIAS=dns \
  -e DNS_SERVICE_PORT=5353 \
  -e TARGET_SERVICE_NAME=server \
  -e TARGET_SERVICE_PORT=8000 \
  -e API_BASE_URL=http://server:8000 \
  -e BROWSER_API_URL=http://${MANAGER_IP}:8000 \
  --restart unless-stopped \
  file-search-client:latest

echo "=== Despliegue en Worker completado ==="
```

### Uso de los scripts

```bash
# En PC1 (Manager)
chmod +x deploy-manager.sh
./deploy-manager.sh

# En PC2 (Worker)
chmod +x deploy-worker.sh
./deploy-worker.sh 192.168.1.100  # IP del manager
```

### Comandos para limpiar

```bash
# Detener y eliminar todos los contenedores
docker stop dns_1 dns_2 dns_3 server client 2>/dev/null
docker rm dns_1 dns_2 dns_3 server client 2>/dev/null

# Eliminar la red
docker network rm file-search-net 2>/dev/null
```

---

## Verificación del Sistema

### 1. Verificar clúster DNS

```bash
# Estado del clúster
curl -s http://localhost:5353/cluster-status | python3 -m json.tool

# Lista de servidores DNS
curl -s http://localhost:5353/dns-servers | python3 -m json.tool

# Health check
curl -s http://localhost:5353/health | python3 -m json.tool
curl -s http://localhost:5354/health | python3 -m json.tool
curl -s http://localhost:5355/health | python3 -m json.tool
```

### 2. Probar resolución DNS

```bash
# Resolver el servidor API
curl -s http://localhost:5353/resolve/server | python3 -m json.tool

# Resolver el cliente
curl -s http://localhost:5353/resolve/client | python3 -m json.tool
```

### 3. Probar failover

```bash
# 1. Ver quién es el primario actual
curl -s http://localhost:5353/cluster-status | grep -E '"role"|"server_id"'

# 2. Detener el primario
docker stop dns_1

# 3. Esperar promoción (5-10 segundos)
sleep 10

# 4. Verificar nuevo primario
curl -s http://localhost:5354/cluster-status | python3 -m json.tool

# 5. Reiniciar el primario original
docker start dns_1

# 6. Verificar reintegración
sleep 15
curl -s http://localhost:5353/cluster-status | python3 -m json.tool
```

### 4. Acceder a la interfaz web

```bash
# Abrir en navegador
# http://localhost:8501  (o http://<IP_WORKER>:8501 si es remoto)
```

### 5. Probar API REST

```bash
# Listar archivos
curl -s http://localhost:8000/files | python3 -m json.tool

# Buscar archivos
curl -s "http://localhost:8000/files?search=test" | python3 -m json.tool
```

---

## Troubleshooting

### Los DNS no se descubren entre sí

```bash
# Verificar que todos comparten el alias 'dns'
docker inspect dns_1 | grep -A5 "Networks"
docker inspect dns_2 | grep -A5 "Networks"

# Probar resolución del alias desde un contenedor
docker exec dns_1 getent hosts dns

# Ver logs de descubrimiento
docker logs dns_1 2>&1 | grep -i "descubr\|discover"
```

### El primario no se promueve automáticamente

```bash
# Verificar health checks
docker logs dns_2 2>&1 | grep -i "health\|promov"

# Verificar conectividad entre contenedores
docker exec dns_2 curl -s http://dns_1:5353/health
```

### El cliente no puede conectar al servidor

```bash
# Verificar que el cliente puede resolver 'server'
docker exec client curl -s http://dns:5353/resolve/server

# Verificar conectividad directa
docker exec client curl -s http://server:8000/health
```

### Problemas de red en Swarm

```bash
# Verificar red overlay
docker network inspect file-search_file_search_net

# Ver routing mesh
docker service inspect file-search_dns_1 --format '{{json .Endpoint}}'

# Recrear la red si es necesario
docker network rm file-search_file_search_net
docker stack deploy -c stack.yml file-search
```

---

## Resumen de Arquitectura

```
┌─────────────────────────────────────────────────────────────────┐
│                         PC1 (Manager)                            │
│  ┌─────────────┐  ┌─────────────┐                               │
│  │    dns_1    │  │   server    │                               │
│  │   :5353     │  │   :8000     │                               │
│  │  (PRIMARY)  │  │   (API)     │                               │
│  └──────┬──────┘  └──────┬──────┘                               │
│         │                │                                       │
│         └────────┬───────┘                                       │
│                  │                                               │
└──────────────────┼───────────────────────────────────────────────┘
                   │
        ═══════════╪═══════════  Red Overlay (file-search-net)
                   │
┌──────────────────┼───────────────────────────────────────────────┐
│                  │              PC2 (Worker)                      │
│         ┌────────┴────────┐                                      │
│         │                 │                                      │
│  ┌──────┴──────┐  ┌──────┴──────┐  ┌─────────────┐              │
│  │    dns_2    │  │    dns_3    │  │   client    │              │
│  │   :5354     │  │   :5355     │  │   :8501     │              │
│  │  (BACKUP)   │  │  (BACKUP)   │  │   (WEB)     │              │
│  └─────────────┘  └─────────────┘  └─────────────┘              │
│                                                                  │
└──────────────────────────────────────────────────────────────────┘

Alias compartido: "dns" → Resuelve a cualquiera de los 3 DNS
Descubrimiento: Dinámico via Docker DNS
Failover: Automático en 5-10 segundos
```

---

## Variables de Entorno

| Variable | Descripción | Valor por defecto |
|----------|-------------|-------------------|
| `DNS_SERVER_ID` | Identificador único del servidor DNS | `dns_{hostname}` |
| `DNS_PORT` | Puerto del servicio DNS | `5353` |
| `DNS_ALIAS` | Alias compartido para descubrimiento | `dns` |
| `HEALTH_CHECK_INTERVAL` | Intervalo de verificación de salud (segundos) | `5` |
| `SYNC_INTERVAL` | Intervalo de sincronización de cache (segundos) | `30` |
| `DISCOVERY_INTERVAL` | Intervalo de re-descubrimiento (segundos) | `15` |
| `LOG_DIR` | Directorio de logs | `/app/logs` |
| `LOG_LEVEL` | Nivel de logging | `INFO` |

---

*Última actualización: Noviembre 2025*
