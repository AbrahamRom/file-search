# Descubrimiento Automático para Descargas de Archivos

## Problema Identificado

Las descargas de archivos solo funcionaban cuando el cliente tenía la variable `BROWSER_API_URL` hardcodeada al puerto correcto del nodo específico:
- `BROWSER_API_URL=http://localhost:8000` → funcionaba en el nodo con processor_1
- `BROWSER_API_URL=http://localhost:8001` → funcionaba en el nodo con processor_2

Esto era problemático porque:
1. No era escalable - cada vez que se agregaba un processor, había que actualizar la configuración
2. No permitía alta disponibilidad - si un processor fallaba, las descargas dejaban de funcionar
3. No funcionaba en diferentes nodos del swarm sin reconfiguración manual

## Solución Implementada

Se implementó un **descubrimiento automático de processors disponibles** que permite al cliente:
1. Consultar al DNS service por la lista de processors disponibles
2. Seleccionar dinámicamente un processor activo para cada descarga
3. Usar el puerto correcto (externo) para construir URLs accesibles desde el navegador

### Componentes Modificados

#### 1. DNS Service (`app/dns_service/main.py`)

**Cambios:**
- Agregado campo `external_port` al modelo `ProcessorRegisterRequest`
- Actualizado el almacenamiento de processors para incluir `external_port`
- Modificado el endpoint `/processor/list` para devolver `external_url` (construido con localhost:PUERTO_EXTERNO)

**Ejemplo de respuesta de `/processor/list`:**
```json
{
  "processors": [
    {
      "processor_id": "processor_1",
      "ip": "processor_1",
      "port": 8000,
      "external_port": 8000,
      "url": "http://processor_1:8000",
      "external_url": "http://localhost:8000",
      "alive": true,
      "seconds_since_heartbeat": 2
    },
    {
      "processor_id": "processor_2",
      "ip": "processor_2",
      "port": 8000,
      "external_port": 8001,
      "url": "http://processor_2:8000",
      "external_url": "http://localhost:8001",
      "alive": true,
      "seconds_since_heartbeat": 3
    }
  ],
  "total": 2,
  "active": 2
}
```

#### 2. Processor (`app/processor/api/endpoints.py`)

**Cambios:**
- Agregada variable de entorno `PROCESSOR_EXTERNAL_PORT` para indicar el puerto externo
- Actualizado el registro con DNS para incluir el `external_port`

**Configuración necesaria:**
```bash
PROCESSOR_EXTERNAL_PORT=8000  # Para processor_1
PROCESSOR_EXTERNAL_PORT=8001  # Para processor_2
```

#### 3. Cliente (`app/client/app.py`)

**Cambios:**
- Nueva función `_get_available_processors()`: consulta al DNS por la lista de processors activos
- Modificada función `_get_processor_download_url()`: selecciona aleatoriamente un processor disponible
- Actualizada función `build_download_url()`: usa descubrimiento dinámico en lugar de `BROWSER_API_URL` hardcodeada

**Comportamiento:**
- El cliente consulta al DNS cada 15 segundos (TTL del cache)
- Para cada descarga, selecciona aleatoriamente un processor disponible (load balancing)
- Usa `external_url` del processor para construir la URL de descarga
- Si no puede contactar al DNS, usa `BROWSER_API_URL` como fallback

#### 4. Docker Compose (`docker-compose.separated.yml`)

**Cambios:**
- Agregada variable `PROCESSOR_EXTERNAL_PORT` en processor_1 (valor: 8000)
- Agregada variable `PROCESSOR_EXTERNAL_PORT` en processor_2 (valor: 8001)

## Cómo Funciona

### Flujo Completo

1. **Inicio del Processor:**
   ```
   Processor → DNS: POST /processor/register
   {
     "processor_id": "processor_1",
     "ip": "processor_1",
     "port": 8000,
     "external_port": 8000
   }
   ```

2. **Cliente solicita descarga:**
   ```
   Cliente → DNS: GET /processor/list
   DNS → Cliente: Lista de processors activos con external_url
   Cliente: Selecciona aleatoriamente un processor
   Cliente: Construye URL: http://localhost:8000/files/{file_id}/download
   ```

3. **Navegador descarga archivo:**
   ```
   Browser → Processor (localhost:8000): GET /files/{file_id}/download
   Processor → DNS: Resuelve storage activo
   Processor → Storage: Descarga archivo
   Storage → Processor → Browser: Stream del archivo
   ```

## Ventajas

1. **Alta Disponibilidad:** Si un processor falla, el cliente automáticamente usa otro
2. **Load Balancing:** Las descargas se distribuyen aleatoriamente entre processors activos
3. **Escalabilidad:** Agregar nuevos processors no requiere reconfiguración del cliente
4. **Zero Configuration:** El cliente se autoconfigura consultando al DNS
5. **Multi-nodo:** Funciona en cualquier nodo del swarm sin cambios

## Uso con Docker Run

Si ejecutas los contenedores con `docker run`, asegúrate de incluir `PROCESSOR_EXTERNAL_PORT`:

```bash
# Processor 1
docker run -d \
  --name processor_1 \
  --network file_search_net \
  -p 8000:8000 \
  -e PROCESSOR_ID=processor_1 \
  -e PROCESSOR_PORT=8000 \
  -e PROCESSOR_EXTERNAL_PORT=8000 \
  -e DNS_ALIAS=dns \
  -e DNS_SERVICE_PORT=5353 \
  file-search-processor:latest

# Processor 2
docker run -d \
  --name processor_2 \
  --network file_search_net \
  -p 8001:8000 \
  -e PROCESSOR_ID=processor_2 \
  -e PROCESSOR_PORT=8000 \
  -e PROCESSOR_EXTERNAL_PORT=8001 \
  -e DNS_ALIAS=dns \
  -e DNS_SERVICE_PORT=5353 \
  file-search-processor:latest

# Cliente (ya no necesita BROWSER_API_URL específica)
docker run -d \
  --name client \
  --network file_search_net \
  -p 8501:8501 \
  -e DNS_ALIAS=dns \
  -e DNS_SERVICE_PORT=5353 \
  -e TARGET_SERVICE_NAME=processor \
  file-search-client:latest
```

## Pruebas

### Verificar que funciona:

1. **Listar processors disponibles desde el DNS:**
   ```bash
   curl http://localhost:5353/processor/list
   ```

2. **Verificar que el cliente puede resolver processors:**
   - Acceder a la interfaz web en http://localhost:8501
   - Buscar archivos y hacer clic en "Descargar"
   - La descarga debe funcionar independientemente del puerto

3. **Probar failover:**
   ```bash
   # Detener processor_1
   docker stop processor_1
   
   # La descarga debe seguir funcionando usando processor_2
   # Verificar logs del cliente para ver el cambio
   docker logs client
   ```

## Troubleshooting

### Las descargas no funcionan

1. **Verificar que los processors están registrados:**
   ```bash
   curl http://localhost:5353/processor/list | jq
   ```

2. **Verificar logs del cliente:**
   ```bash
   docker logs client | grep processor
   ```

3. **Verificar que `external_port` está configurado:**
   ```bash
   docker inspect processor_1 | grep PROCESSOR_EXTERNAL_PORT
   ```

### Cache no se actualiza

El cliente cachea la lista de processors por 15 segundos. Si agregaste un nuevo processor y no aparece:
- Espera 15 segundos y recarga la página
- O reinicia el cliente: `docker restart client`

## Próximos Pasos

Posibles mejoras futuras:
1. **Sticky Sessions:** Mantener el mismo processor durante una sesión de descarga
2. **Health Checks:** El cliente puede verificar la salud del processor antes de usarlo
3. **Métricas:** Tracking de qué processor maneja más descargas
4. **Geolocalización:** Seleccionar el processor más cercano al cliente
