# 🌐 Guía para Configurar IPs de Host en File-Search

Esta guía explica **cómo obtener y usar correctamente las IPs de tus hosts** para que las descargas funcionen desde cualquier navegador en tu red.

---

## 🎯 ¿Por qué necesitamos las IPs de los hosts?

Cuando usas Docker en múltiples hosts (máquinas físicas), cada contenedor tiene:
- **IP interna de Docker** (ej: `10.0.1.32`) - Solo accesible dentro de Docker
- **IP del host físico** (ej: `192.168.163.211`) - Accesible desde cualquier navegador en la red

**El problema:** Si usamos la IP interna de Docker, el navegador no puede descargar archivos porque esa IP no es accesible fuera de Docker.

**La solución:** Configurar `PROCESSOR_EXTERNAL_IP` con la IP del host físico.

---

## 📍 PASO 1: Obtener las IPs de tus hosts

### En Linux/Mac:

```bash
# Método más simple (recomendado)
hostname -I | awk '{print $1}'

# Ejemplo de salida:
# 192.168.163.211
```

**Otros métodos:**
```bash
# Ver todas las interfaces
ip addr show | grep "inet " | grep -v 127.0.0.1

# IP de interfaz específica (cambiar eth0 por tu interfaz)
ip addr show eth0 | grep "inet " | awk '{print $2}' | cut -d/ -f1
```

### En Windows (PowerShell):

```powershell
# Obtener IP principal
(Get-NetIPAddress -AddressFamily IPv4 -InterfaceAlias Ethernet).IPAddress

# Ver todas las IPs
ipconfig | findstr IPv4
```

### ✅ Verificación:

La IP debe tener uno de estos formatos:
- `192.168.x.x` (red doméstica común)
- `10.x.x.x` (red privada grande)
- `172.16.x.x` a `172.31.x.x` (red privada mediana)

❌ **NO uses:**
- `127.0.0.1` (localhost - solo local)
- `10.0.x.x` si parece ser una red Docker (comienza con 10.0)

---

## 📝 PASO 2: Guardar tus IPs

Una vez obtenidas las IPs, guárdalas:

```bash
# Ejemplo para 2 hosts:
Host 1 (Manager): 192.168.163.211
Host 2 (Worker):  192.168.163.212
```

**Verificar conectividad entre hosts:**
```bash
# Desde Host 1, hacer ping a Host 2:
ping 192.168.163.212

# Desde Host 2, hacer ping a Host 1:
ping 192.168.163.211
```

---

## 🚀 PASO 3: Usar las IPs en los comandos Docker

### Para `comandos Host 1.txt`:

Reemplaza `PROCESSOR_EXTERNAL_IP` con la IP del Host 1:

```bash
docker run -d \
  --name processor_1 \
  --hostname processor_1 \
  --network file_search_net \
  --network-alias processor \
  -p 8000:8000 \
  -v /srv/file-search/logs:/app/logs \
  -e PROCESSOR_ID=processor_1 \
  -e PROCESSOR_PORT=8000 \
  -e PROCESSOR_EXTERNAL_PORT=8000 \
  -e PROCESSOR_EXTERNAL_IP=192.168.163.211 \  # ← IP del Host 1
  -e STORAGE_NODES=http://storage_1:8000,http://storage_2:8000,http://storage_3:8000 \
  -e STORAGE_URL=http://storage_1:8000 \
  -e DNS_ALIAS=dns \
  -e DNS_SERVICE_PORT=5353 \
  -e STORAGE_TIMEOUT=10 \
  -e STORAGE_MAX_RETRIES=3 \
  -e STORAGE_RETRY_DELAY=0.5 \
  -e LOG_DIR=/app/logs \
  file-search-processor:latest
```

### Para `comandos Host 2.txt`:

Reemplaza `PROCESSOR_EXTERNAL_IP` con la IP del Host 2:

```bash
docker run -d \
  --name processor_2 \
  --hostname processor_2 \
  --network file_search_net \
  --network-alias processor \
  -p 8001:8000 \
  -v /srv/file-search/logs:/app/logs \
  -e PROCESSOR_ID=processor_2 \
  -e PROCESSOR_PORT=8000 \
  -e PROCESSOR_EXTERNAL_PORT=8001 \
  -e PROCESSOR_EXTERNAL_IP=192.168.163.212 \  # ← IP del Host 2
  -e STORAGE_NODES=http://storage_1:8000,http://storage_2:8000,http://storage_3:8000 \
  -e STORAGE_URL=http://storage_1:8000 \
  -e DNS_ALIAS=dns \
  -e DNS_SERVICE_PORT=5353 \
  -e STORAGE_TIMEOUT=10 \
  -e STORAGE_MAX_RETRIES=3 \
  -e STORAGE_RETRY_DELAY=0.5 \
  -e LOG_DIR=/app/logs \
  file-search-processor:latest
```

---

## 🔍 PASO 4: Verificar que funciona

### 1. Consultar el DNS para ver las IPs registradas:

```bash
# Desde cualquier host:
curl http://192.168.163.211:5353/processor/list
```

**Deberías ver:**
```json
{
  "processors": [
    {
      "processor_id": "processor_1",
      "external_ip": "192.168.163.211",  // ← IP del Host 1
      "external_port": 8000,
      "external_url": "http://192.168.163.211:8000"  // ← URL accesible
    },
    {
      "processor_id": "processor_2",
      "external_ip": "192.168.163.212",  // ← IP del Host 2
      "external_port": 8001,
      "external_url": "http://192.168.163.212:8001"  // ← URL accesible
    }
  ]
}
```

### 2. Probar descarga directa desde navegador:

Abre en tu navegador:
```
http://192.168.163.211:8000/files/<FILE_ID>/download
http://192.168.163.212:8001/files/<FILE_ID>/download
```

✅ **Debe descargar el archivo correctamente**

---

## 🐳 Uso con Docker Compose

Si usas `docker-compose.separated.yml`, crea un archivo `.env`:

```bash
# .env
PROCESSOR_1_EXTERNAL_IP=192.168.163.211
PROCESSOR_2_EXTERNAL_IP=192.168.163.212
```

Luego ejecuta:
```bash
docker-compose -f docker-compose.separated.yml up -d
```

---

## 📦 Uso con Docker Swarm Stack

Si usas `stack.separated.yml`:

```bash
# Exportar variables antes de desplegar
export PROCESSOR_1_EXTERNAL_IP=192.168.163.211
export PROCESSOR_2_EXTERNAL_IP=192.168.163.212

# Desplegar stack
docker stack deploy -c stack.separated.yml file-search
```

---

## ❓ Troubleshooting

### Problema: URLs de descarga siguen mostrando `10.0.x.x`

**Causa:** No configuraste `PROCESSOR_EXTERNAL_IP` o el processor no se ha re-registrado.

**Solución:**
```bash
# 1. Rebuild de la imagen processor
docker build -t file-search-processor:latest -f Dockerfile.processor .

# 2. Detener y eliminar processor
docker stop processor_1 && docker rm processor_1

# 3. Volver a crear CON la variable PROCESSOR_EXTERNAL_IP
docker run -d ... -e PROCESSOR_EXTERNAL_IP=192.168.163.211 ... file-search-processor:latest
```

### Problema: No puedo descargar desde otro host

**Verificar:**
1. ¿La IP es accesible desde el navegador?
   ```bash
   # Desde tu navegador, prueba:
   curl http://192.168.163.211:8000/health
   ```

2. ¿El firewall está bloqueando el puerto?
   ```bash
   # En el host, verificar puertos abiertos:
   sudo ss -tlnp | grep 8000
   ```

3. ¿El processor está usando la IP correcta?
   ```bash
   docker logs processor_1 | grep "registrado"
   # Debe mostrar: externo:192.168.163.211:8000
   ```

### Problema: Obtengo IP `10.0.x.x` con `hostname -I`

**Causa:** La primera IP es la de Docker, no la del host.

**Solución:**
```bash
# Ver TODAS las interfaces:
ip addr show

# Buscar tu interfaz de red física (ej: eth0, ens33, enp0s3)
# y obtener su IP:
ip addr show eth0 | grep "inet " | awk '{print $2}' | cut -d/ -f1
```

---

## 📚 Resumen

| Variable | Descripción | Ejemplo |
|----------|-------------|---------|
| `PROCESSOR_EXTERNAL_IP` | IP del host físico | `192.168.163.211` |
| `PROCESSOR_EXTERNAL_PORT` | Puerto expuesto en el host | `8000` o `8001` |
| `external_url` | URL final de descarga | `http://192.168.163.211:8000` |

**Resultado final:** Las URLs de descarga usan IPs accesibles desde cualquier navegador en tu red. 🎉
