# Implementación: Estrategia Híbrida de Descubrimiento DNS Sin Configuración Especial

## 📋 Resumen Ejecutivo

Se ha implementado una **estrategia híbrida modular** para descubrimiento de servidores DNS con los siguientes componentes:

✅ **Opción 1: Cache Persistente** (implementado previamente)
- Guardar resoluciones en `/tmp/dns-cache/dns_cache.json`
- Usar caché expirado como último recurso

✅ **Opción 2: Descubrimiento por Rango de IPs** (nueva implementación)
- Auto-detección de red local
- Escaneo paralelo de puertos TCP
- Identificación de servicios mediante health checks HTTP
- **Sin requerir configuración especial de Docker**

---

## 🏗️ Arquitectura Modular

### **Nuevo Módulo: `app/common/ip_discovery.py`**

Proporciona dos clases principales:

#### **1. IPRangeDiscovery**
Encapsula toda la lógica de descubrimiento de red:

```python
discovery = IPRangeDiscovery(timeout=0.5, max_workers=20)

# Módulo 1: Detección de red local
network = discovery.detect_local_network()  # "172.18.0.0/24"

# Módulo 2: Escaneo de puertos
results = discovery.scan_quick_ports(network, ports=[5353, 8000])
# {5353: ["172.18.0.5", "172.18.0.6"], 8000: ["172.18.0.10"]}

# Módulo 3: Identificación de servicios
services = discovery.identify_service_batch(results)
# [{ip: "172.18.0.5", port: 5353, type: "dns", server_id: "dns_1", ...}]

# Módulo 4: Ping sweep (opcional)
active = discovery.discover_with_ping(network)
# ["172.18.0.5", "172.18.0.6", ...]
```

#### **2. DiscoveryManager**
Orquesta las estrategias y maneja caché:

```python
manager = DiscoveryManager(dns_port=5353)

# Descubrimiento con caché automático
dns_servers = manager.discover_dns_servers(
    network_hint=None,        # Auto-detectar red
    force_refresh=False,      # Usar cache si es fresco
    use_ping_sweep=False      # No usar ping sweep (más rápido)
)

# Descubrir todos los servicios
all_services = manager.discover_all_services()
# {"dns": [...], "storage": [...], "processor": [...], "unknown": [...]}

# Limpiar cache
manager.clear_cache()
```

---

## 🔄 Flujo de Descubrimiento Completo (Integrado en DNSClientHA)

```
┌────────────────────────────────────────────────────────────┐
│ 1. resolve(hostname)                                        │
│    - Cache en memoria (TTL válido) ✅                      │
└──────────────────┬─────────────────────────────────────────┘
                   ↓ Si no está en cache
┌────────────────────────────────────────────────────────────┐
│ 2. Verificar si bootstrapeado                               │
│    - Si NO → Ejecutar bootstrap()                           │
└──────────────────┬─────────────────────────────────────────┘
                   ↓
┌────────────────────────────────────────────────────────────┐
│ 3. _bootstrap() - Inicialización                            │
│                                                             │
│   Intento 1: Alias DNS de Docker                           │
│   - socket.getaddrinfo("dns", 5353)                        │
│   - Si ✅ → bootstrap exitoso                              │
│   - Si ❌ → continuar                                       │
│                                                             │
│   Intento 2: IP Range Discovery (NUEVO)                    │
│   - detect_local_network() → "172.18.0.0/24"              │
│   - scan_quick_ports() → 2-5 segundos                     │
│   - identify_service_batch()                               │
│   - Si ✅ → bootstrap exitoso                              │
│   - Si ❌ → continuar                                       │
└──────────────────┬─────────────────────────────────────────┘
                   ↓
┌────────────────────────────────────────────────────────────┐
│ 4. Intentar resolución con servidores conocidos             │
│    - Primario primero                                       │
│    - Healthy secundarios                                    │
│    Si ✅ → Guardar cache + cache persistente               │
└──────────────────┬─────────────────────────────────────────┘
                   ↓ Si todos fallan
┌────────────────────────────────────────────────────────────┐
│ 5. Re-bootstrap (_attempt_rebootstrap)                      │
│    - Intento 1: Docker DNS                                 │
│    - Intento 2: IP Range Discovery                         │
│    Si ✅ → reintentar resoluciones                         │
└──────────────────┬─────────────────────────────────────────┘
                   ↓ Si re-bootstrap falla
┌────────────────────────────────────────────────────────────┐
│ 6. Fallback a Cache Persistente (aunque expirado)          │
│    - Buscar en /tmp/dns-cache/dns_cache.json              │
│    - Si encontrado → Retornar IP                          │
└──────────────────┬─────────────────────────────────────────┘
                   ↓ Si cache vacío
┌────────────────────────────────────────────────────────────┐
│ 7. Fallback a Docker DNS Nativo                            │
│    - socket.gethostbyname(hostname)                        │
│    - Si ✅ → Guardar en cache persistente                  │
└────────────────────────────────────────────────────────────┘
```

---

## 📊 Integración en `app/common/resolver.py`

### **Cambios en la clase `DNSClientHA`**

#### **1. Inicialización**
```python
def __init__(self, dns_alias: str = "dns", dns_port: int = 5353):
    # ... código existente ...
    
    # Discovery por rango de IPs (fallback)
    self._ip_discovery = DiscoveryManager(dns_port=dns_port)
    self._network_hint = os.getenv("NETWORK_CIDR", None)  # Opcional
    
    # Bootstrap inicial
    self._bootstrap()
```

#### **2. Método `_bootstrap()` mejorado**
```python
def _bootstrap(self) -> None:
    """
    Inicializa el cliente DNS con estrategia híbrida:
    1. Intenta descubrir via alias DNS de Docker
    2. Si falla, usa descubrimiento por rango de IPs
    """
    # Método 1: Alias DNS de Docker (existente)
    discovered_ips = self._discover_via_alias()
    if discovered_ips:
        # ... procesamiento normal ...
        return
    
    # Método 2: Fallback a IP Range Discovery (NUEVO)
    logger.warning("[DNSClientHA] Alias DNS de Docker no disponible...")
    if self._bootstrap_via_ip_range():
        return
    
    self._bootstrapped = False
```

#### **3. Nuevo método `_bootstrap_via_ip_range()`**
```python
def _bootstrap_via_ip_range(self) -> bool:
    """
    Fallback: Descubre servidores DNS usando escaneo de rango de IPs.
    """
    logger.warning("[DNSClientHA] Fallback a descubrimiento por rango de IPs...")
    
    discovered = self._ip_discovery.discover_dns_servers(
        network_hint=self._network_hint,
        force_refresh=True,
        use_ping_sweep=False  # Escaneo rápido
    )
    
    if not discovered:
        return False
    
    # Procesar resultados y construir lista de servidores
    # ... código de integración ...
    
    return True
```

#### **4. Método `_attempt_rebootstrap()` mejorado**
```python
def _attempt_rebootstrap(self) -> bool:
    """
    Orden de intento:
    1. Alias DNS de Docker
    2. IP Range Discovery
    3. Usar servidores conocidos previos
    """
    # Intento 1: Docker DNS
    self._bootstrap()
    if self._bootstrapped:
        return True
    
    # Intento 2: IP Range Discovery
    logger.info("[DNSClientHA] Intentando IP range discovery...")
    return self._bootstrap_via_ip_range()
```

---

## ⚡ Características del Descubrimiento por Rango de IPs

### **Velocidades Esperadas**
- **Auto-detección de red**: < 0.1 segundos
- **Escaneo rápido (50 hosts, 2 puertos)**: 2-5 segundos
- **Identificación de servicios**: 1-2 segundos
- **Total sin ping sweep**: 3-7 segundos (aceptable para fallback)

### **Ventajas**
✅ No requiere configuración especial de Docker (`--cap-add`, etc.)
✅ No requiere herramientas externas en la imagen
✅ Auto-detecta la red local automáticamente
✅ Parallelización eficiente (ThreadPoolExecutor)
✅ Identifica tipos de servicio correctamente
✅ Cache de descubrimiento (5 minutos)
✅ Fallback graceful a Docker DNS

### **Limitaciones**
⚠️ Toma 3-7 segundos (Docker DNS alias es más rápido)
⚠️ Requiere acceso de red a la red local
⚠️ Limitado a redes /24 (256 hosts máximo)
⚠️ Si todos los servicios están en puertos diferentes, necesita escanear más

---

## 🔌 Configuración (Opcional)

### **Sin configuración especial (recomendado)**
```bash
docker run -d \
  --name processor_1 \
  --network file_search_net \
  -p 8000:8000 \
  file-search-processor:latest
  # ← Funciona automáticamente, detecta red local
```

### **Con sugerencia de red CIDR (si la auto-detección falla)**
```bash
docker run -d \
  --name processor_1 \
  --network file_search_net \
  -e NETWORK_CIDR="172.18.0.0/16" \
  -p 8000:8000 \
  file-search-processor:latest
```

### **Con ping sweep habilitado (más lento pero más completo)**
Solo modificar en código el parámetro `use_ping_sweep=True` en `_bootstrap_via_ip_range()`.

---

## 📝 Ejemplo de Logs

### **Bootstrap exitoso via Docker DNS**
```
[DNSClientHA] Iniciando bootstrap con alias 'dns'...
[IPDiscovery] Red local detectada: 172.18.0.0/24 (IP local: 172.18.0.2)
[DNSClientHA] IPs descubiertas: ['172.18.0.5', '172.18.0.6']
[DNSClientHA] Bootstrap exitoso. 3 servidores conocidos
```

### **Fallback a IP Range Discovery**
```
[DNSClientHA] Iniciando bootstrap con alias 'dns'...
[DNSClientHA] Alias DNS de Docker no disponible. Intentando descubrimiento...
[IPDiscovery] Red local detectada: 172.18.0.0/24 (IP local: 172.18.0.2)
[DiscoveryManager] Iniciando descubrimiento en 172.18.0.0/24
[IPDiscovery] Escaneo rápido: 50 hosts, 2 puertos
[IPDiscovery] Escaneo completado en 4.23s. Encontrados: 5 hosts
[IPDiscovery] Identificando tipos de servicio...
[IPDiscovery] Identificación completada: 5 hosts analizados
[DiscoveryManager] Descubrimiento completado en 5.12s: 2 DNS encontrados
[DNSClientHA] Bootstrap via IP range completado
```

### **Uso de Cache Persistente**
```
[DNSClientHA] Todos los servidores DNS fallaron. Intentando re-bootstrap...
[DiscoveryManager] Usando cache: 3 servidores DNS
[DNSClientHA] Re-bootstrap exitoso con cache
```

---

## 🧪 Pruebas Manuales

### **1. Verificar que los módulos se cargan correctamente**
```bash
cd /home/abraham/Escritorio/SD/file-search
python3 -c "from app.common.resolver import DNSClientHA; from app.common.ip_discovery import DiscoveryManager; print('✅ Módulos OK')"
```

### **2. Probar descubrimiento de red local**
```bash
python3 << 'EOF'
from app.common.ip_discovery import IPRangeDiscovery

discovery = IPRangeDiscovery()
network = discovery.detect_local_network()
print(f"Red detectada: {network}")

if network:
    results = discovery.scan_quick_ports(network)
    print(f"Puertos encontrados: {results}")
EOF
```

### **3. Probar DiscoveryManager completo**
```bash
python3 << 'EOF'
from app.common.ip_discovery import DiscoveryManager

manager = DiscoveryManager()
dns_servers = manager.discover_dns_servers(force_refresh=True)
print(f"Servidores DNS encontrados: {len(dns_servers)}")
for srv in dns_servers:
    print(f"  - {srv['ip']}:{srv['port']} (tipo: {srv['type']})")
EOF
```

---

## 📦 Dependencias

### **Módulos nuevos requeridos**
- `ipaddress` (built-in en Python 3.3+)
- `concurrent.futures` (built-in)
- `subprocess` (built-in)
- `socket` (built-in)
- `threading` (built-in)
- `requests` (ya requerido por resolver.py)

✅ **No se requieren dependencias adicionales**

---

## 🔐 Consideraciones de Seguridad

1. **Acceso a red local**: El escaneo requiere acceso a la red Docker
   - Seguro dentro de `--network file_search_net`
   - No expone puertos al exterior

2. **Health checks**: Los health checks HTTP son read-only
   - Solo GET requests a `/health`, `/node/status`, etc.
   - No modifican estado

3. **Timeouts agresivos**: Conexiones TCP con timeout 0.5s
   - Previene bloqueos
   - Rápido fallback

---

## 🎯 Próximos Pasos (Opcional)

Si necesitas mejor rendimiento:

1. **Agregar ARP scanning** (requiere `--cap-add=NET_ADMIN`)
   - Mucho más rápido que escaneo TCP
   - Pero requiere configuración especial

2. **Persistencia del discovery cache** (como cache DNS)
   - Guardar descubrimientos en `/tmp/dns-discovery-cache.json`
   - Recuperar después de reinicio

3. **Métricas de descubrimiento**
   - Tiempo total del descubrimiento
   - Precisión de identificación
   - Tasa de uso del fallback

---

## ✅ Checklist de Verificación

- [x] Módulo `ip_discovery.py` creado
- [x] Integración en `resolver.py`
- [x] Sin configuración especial de Docker
- [x] Sintaxis Python válida
- [x] Módulos importables
- [x] Cache de descubrimiento
- [x] Documentación completa
- [ ] Tests unitarios (opcional)
- [ ] Benchmarks de velocidad (opcional)

---

## 📖 Uso en Aplicación

El cliente DNS se usa igual que antes, pero ahora con fallback automático:

```python
from app.common.resolver import DNSClientHA

# Crear cliente (bootstrap automático)
client = DNSClientHA(dns_alias="dns", dns_port=5353)

# Resolver hostname (con fallback automático)
try:
    ip = client.resolve("storage_1")
    print(f"Conectar a: {ip}:8000")
except socket.gaierror:
    print("No se pudo resolver hostname")
```

**Orden de resolución automático:**
1. Cache en memoria (ms)
2. Servidores DNS conocidos (100-500ms)
3. Re-bootstrap + cache persistente (3-7 segundos)
4. Docker DNS nativo (1-2 segundos)

---

¡Implementación completada! 🎉
