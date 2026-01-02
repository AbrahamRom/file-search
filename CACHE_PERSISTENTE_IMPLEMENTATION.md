# Implementación: Cache Persistente de Resoluciones DNS

## 📋 Resumen

Se ha implementado la **Opción 1: Cache Persistente de Resoluciones** en el módulo `DNSClientHA` para mejorar la disponibilidad del sistema cuando el DNS de Docker falla.

### Características Principales

- ✅ **Cache persistente en disco**: Cada contenedor guarda sus resoluciones en `/tmp/dns-cache/dns_cache.json`
- ✅ **Sin volúmenes Docker**: Cada contenedor mantiene su propio caché independiente
- ✅ **Sin variables de entorno**: La ruta `/tmp/dns-cache` se asume por defecto
- ✅ **Auto-creación de directorios**: Se crea automáticamente en el inicio del contenedor
- ✅ **Thread-safe**: Protegido con locks para acceso concurrente
- ✅ **Fallback jerárquico**: Usar caché expirado como último recurso antes de Docker DNS nativo

---

## 🔧 Cambios Implementados

### 1. **Modificaciones en `app/common/resolver.py`**

#### Importes adicionales
```python
import json
from pathlib import Path
```

#### Nueva lógica de inicialización (`__init__`)
```python
# Cache persistente
self._cache_dir = Path("/tmp/dns-cache")
self._cache_file = self._cache_dir / "dns_cache.json"
self._ensure_cache_dir()
self._load_persistent_cache()
```

#### Nuevos métodos

**1. `_ensure_cache_dir()`**
- Crea el directorio `/tmp/dns-cache` si no existe
- Usa `Path.mkdir(parents=True, exist_ok=True)` para manejo robusto
- Registra logs de debug para auditoría

**2. `_load_persistent_cache()`**
- Se ejecuta al iniciar el contenedor
- Carga todas las resoluciones previas del archivo JSON
- Inicializa `self._cache` con los datos persistentes
- Permite que el cliente tenga resoluciones válidas incluso si DNS falló

**3. `_save_persistent_cache()`**
- Guarda el caché actual al disco después de:
  - Cada resolución exitosa (en `_try_resolve()`)
  - Fallback a Docker DNS nativo
  - Re-bootstrap fallido
- Ejecuta sincronía para garantizar persistencia inmediata

#### Cambios en `_attempt_rebootstrap()`
```python
# ❌ ANTES: Limpiaba todo el caché
self._cache = {}

# ✅ AHORA: Preserva el caché persistente
self._save_persistent_cache()
# NO limpiamos self._cache para preservar el caché persistente
```

#### Cambios en `_try_resolve()`
```python
if ip:
    with self._lock:
        self._cache[hostname] = {
            "ip": ip,
            "expires_at": time.time() + ttl,
            "resolved_by": server_id,
            "role": role
        }
    
    # ✅ NUEVO: Guardar caché persistente después de resolución exitosa
    self._save_persistent_cache()
```

#### Cambios en `resolve()`
```python
# ✅ NUEVO: Fallback 6 - Cache persistente expirado
with self._lock:
    cached = self._cache.get(hostname)
    if cached:
        logger.warning(f"[DNSClientHA] Usando caché persistente expirado como último recurso: {hostname} -> {cached['ip']}")
        return cached["ip"]

# ✅ ACTUALIZADO: Fallback 7 - Docker DNS nativo (antes era fallback 6)
# ... código existente ...
self._save_persistent_cache()  # Guardar también el fallback
```

### 2. **Cambios en Dockerfiles**

Todos los Dockerfiles ahora crean el directorio `/tmp/dns-cache`:

#### `Dockerfile.dns` (en `app/dns_service/Dockerfile`)
```dockerfile
RUN mkdir -p /app/logs /tmp/dns-cache
```

#### `Dockerfile.storage`
```dockerfile
RUN mkdir -p /app/files /app/logs /app/data /tmp/source_files /tmp/dns-cache
```

#### `Dockerfile.processor`
```dockerfile
RUN mkdir -p /app/logs /tmp/dns-cache
```

#### `Dockerfile.client`
```dockerfile
RUN mkdir -p /tmp/dns-cache
```

---

## 🔄 Flujo de Resolución de Nombres (Actualizado)

```
┌─────────────────────────────────────────────────────────────────┐
│ 1. resolve(hostname)                                            │
│    - Verificar si está en CACHE en memoria (TTL válido)         │
│      Si ✅ → Retornar IP                                       │
└─────────────────────────────────────────────────────────────────┘
                           ↓
┌─────────────────────────────────────────────────────────────────┐
│ 2. Verificar si cliente está bootstrapeado                      │
│    - Si NO → Ejecutar bootstrap (descubrir servidores DNS)      │
└─────────────────────────────────────────────────────────────────┘
                           ↓
┌─────────────────────────────────────────────────────────────────┐
│ 3. Intentar resolución en el primario DNS                       │
│    Si ✅ → Guardar en cache + caché persistente → Retornar     │
│    Si ❌ → Continuar                                            │
└─────────────────────────────────────────────────────────────────┘
                           ↓
┌─────────────────────────────────────────────────────────────────┐
│ 4. Intentar con cada servidor DNS healthy (en orden)            │
│    Si ✅ → Guardar en cache + caché persistente → Retornar     │
│    Si ❌ → Continuar                                            │
└─────────────────────────────────────────────────────────────────┘
                           ↓
┌─────────────────────────────────────────────────────────────────┐
│ 5. Reintentar con servidores unhealthy                          │
│    Si ✅ → Marcar healthy + Guardar cache → Retornar           │
│    Si ❌ → Continuar                                            │
└─────────────────────────────────────────────────────────────────┘
                           ↓
┌─────────────────────────────────────────────────────────────────┐
│ 6. Re-bootstrap del cliente DNS                                 │
│    - Reintentar con servidores actualizados                     │
│    Si ✅ → Guardar cache → Retornar                            │
│    Si ❌ → Continuar                                            │
└─────────────────────────────────────────────────────────────────┘
                           ↓
┌─────────────────────────────────────────────────────────────────┐
│ 7. ✨ NUEVO: Cache Persistente (ÚLTIMO RECURSO)                 │
│    - Buscar en cache en memoria (aunque esté expirado)          │
│    Si encontrado → Registrar WARNING → Retornar IP             │
│    Si NO → Continuar                                            │
└─────────────────────────────────────────────────────────────────┘
                           ↓
┌─────────────────────────────────────────────────────────────────┐
│ 8. Fallback: Docker DNS nativo                                  │
│    - Usar socket.gethostbyname() como último recurso            │
│    Si ✅ → Guardar en cache + caché persistente → Retornar     │
│    Si ❌ → Lanzar excepción                                     │
└─────────────────────────────────────────────────────────────────┘
```

---

## 📊 Formato del Caché Persistente

**Archivo**: `/tmp/dns-cache/dns_cache.json`

```json
{
  "hostname_cache": {
    "storage_1": {
      "ip": "172.18.0.10",
      "expires_at": 1704156789.123,
      "resolved_by": "dns_1",
      "role": "primary"
    },
    "storage_2": {
      "ip": "172.18.0.11",
      "expires_at": 1704156789.456,
      "resolved_by": "dns_1",
      "role": "primary"
    },
    "processor_1": {
      "ip": "172.18.0.20",
      "expires_at": 1704156789.789,
      "resolved_by": "dns_2",
      "role": "backup"
    }
  },
  "timestamp": 1704156789.950
}
```

---

## 🛡️ Ventajas de esta Implementación

| Aspecto | Beneficio |
|--------|----------|
| **Recuperación ante fallos** | Si DNS está caído, el caché permite seguir funcionando |
| **Sin volúmenes Docker** | Cada contenedor es independiente, no requiere infraestructura adicional |
| **Sin variables de entorno** | Configuración más simple y predecible |
| **Thread-safe** | Uso de locks garantiza consistencia en acceso concurrente |
| **Auto-creación de directorios** | Dockerfile crea `/tmp/dns-cache` automáticamente |
| **Persistencia rápida** | Se guarda en `/tmp/` (memoria RAM o SSD rápida) |
| **Auditoría de logs** | Logs indican cuándo se usan cachés expirados |

---

## ⚠️ Consideraciones Importantes

1. **Almacenamiento temporal**: `/tmp/dns-cache` está en memoria tmpfs o SSD rápida
   - Se persiste durante la vida del contenedor
   - Se pierde cuando el contenedor se reinicia

2. **Datos expirados**: El caché persistente se usa **aunque esté expirado**
   - Esto es intencional como último recurso
   - Registra WARNING en logs cuando ocurre

3. **Sincronía**: `_save_persistent_cache()` se ejecuta **de forma sincrónica**
   - Garantiza que los datos se escriban inmediatamente
   - Pequeño overhead (generalmente <1ms)

4. **Compartir caché entre contenedores**: Si necesitas compartir caché:
   - Actualmente cada contenedor tiene su archivo separado
   - Para compartir: montar un volumen en `/tmp/dns-cache` con rw en todos los contenedores
   - Ejemplo:
     ```bash
     -v /srv/file-search/dns-cache:/tmp/dns-cache
     ```

---

## 🧪 Ejemplo de Uso en Logs

### Resolución Normal
```
[DNSClientHA] Consultando http://172.18.0.5:5353 para 'storage_1'
[DNSClientHA] Resolución OK: storage_1 -> 172.18.0.10 (via dns_1/primary)
[DNSClientHA] Caché persistente guardado: 5 entradas
```

### Caché Válido
```
[DNSClientHA] Cache HIT: storage_1 -> 172.18.0.10
```

### Caché Expirado (Último Recurso)
```
[DNSClientHA] Cache EXPIRED: storage_1
[DNSClientHA] Todos los servidores DNS fallaron. Intentando re-bootstrap...
[DNSClientHA] Usando caché persistente expirado como último recurso: storage_1 -> 172.18.0.10
```

### Docker DNS Nativo
```
[DNSClientHA] Todos los servidores DNS fallaron. Usando fallback nativo para 'storage_1'
[DNSClientHA] Fallback exitoso: storage_1 -> 172.18.0.10
```

---

## ✅ Verificación de la Implementación

Para verificar que todo está funcionando:

```bash
# 1. Construir imágenes
docker build -t file-search-dns:latest -f app/dns_service/Dockerfile .
docker build -t file-search-storage:latest -f Dockerfile.storage .
docker build -t file-search-processor:latest -f Dockerfile.processor .
docker build -t file-search-client:latest -f Dockerfile.client .

# 2. Verificar que /tmp/dns-cache se crea
docker run --rm file-search-storage:latest ls -la /tmp/dns-cache

# 3. Ejecutar un contenedor y verificar caché
docker run -d --name test-dns file-search-dns:latest
docker exec test-dns ls -la /tmp/dns-cache/
docker logs test-dns | grep "Caché persistente"
docker rm -f test-dns
```

---

## 📝 Notas Técnicas

- **Thread-safety**: Todos los accesos a `self._cache` están protegidos con `self._lock`
- **Manejo de errores**: Se capturan excepciones en creación/lectura/escritura de archivos
- **Compatibilidad**: Compatible con Python 3.8+
- **Dependencias**: Solo usa módulos estándar (`json`, `pathlib`, `threading`)
