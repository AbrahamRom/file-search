# Informe Final del Sistema Distribuido - File Search

## Índice

1. Arquitectura
2. Procesos
3. Comunicación
4. Coordinación
5. Nombrado y Localización
6. Consistencia y Replicación
7. Tolerancia a Fallos
8. Seguridad
9. Despliegue
10. Puntos de Entrada
11. Referencias de Código
12. Conceptos
13. PRIMARY–BACKUP y Failover por capa

---

## 1. Arquitectura

- Arquitectura separada en capas con alta disponibilidad:
  - Capa DNS HA: coordinación, descubrimiento y failover de servicios.
  - Capa Storage: nodos con BD SQLite y volumen de archivos, modelo PRIMARY–BACKUP.
  - Capa Processor: nodos stateless que atienden al cliente y enrutan al Storage activo.
  - Cliente Streamlit: interfaz web para buscar, filtrar, subir y descargar.
- Despliegue en Docker Swarm sobre red overlay compartida.
- Separación clara de responsabilidades:
  - DNS decide qué Storage es PRIMARY y sirve resoluciones para Processor/Cliente.
  - Storage gestiona estado (BD/archivos) y replicación entre PRIMARY y BACKUPs.
  - Processor sólo resuelve vía DNS y reintenta con circuit breaker ante fallos.

Diagrama conceptual de capas:

Cliente → Processor (stateless) → DNS HA → Storage (PRIMARY/BACKUP)

---

## 2. Procesos

- DNS Service: 3 réplicas (1 PRIMARY, 2 BACKUP) con alias compartido “dns”.
- Storage Nodes: 3 réplicas con roles dinámicos (1 PRIMARY, 2 BACKUP).
- Processor Nodes: 2 réplicas, stateless, escalables horizontalmente.
- Cliente Web: 1 réplica Streamlit.
- Puertos típicos:
  - DNS: 5353 (ingress 5353/5354/5355 según instancia).
  - Storage: 8000 (publicado como 9000/9001/9002).
  - Processor: 8000 (publicado como 8000/8001).
  - Cliente: 8501.

---

## 3. Comunicación

- HTTP interno entre servicios sobre red overlay.
- Cliente consulta al DNS para resolver un Processor disponible, luego llama al Processor.
- Processor resuelve el Storage activo mediante DNS y enruta las solicitudes.
- Endpoints clave del DNS:
  - /dns-servers: lista de servidores DNS y sus roles/salud.
  - /server/register, /server/heartbeat, /server/resolve: ciclo de vida de Storage y failover.
  - /processor/register, /processor/heartbeat, /processor/resolve: registro y resolución de Processor.
- Endpoints del Storage (consumidos por Processor):
  - /files, /files/{id}: metadata.
  - /internal/db_snapshot, /internal/files: sincronización.
  - /download y endpoints de subida según implementación actual.

---

## 4. Coordinación

- Elección y mantenimiento de PRIMARY en Storage:
  - Registro inicial: primer Storage activo se convierte en PRIMARY.
  - Heartbeats periódicos: si el PRIMARY no responde, el DNS promueve el BACKUP más antiguo vivo.
  - Lease y epoch:
    - primary_epoch monotónico para fencing de escrituras.
    - lease_expires_at para controlar vigencia del PRIMARY.
- Split-brain:
  - Degradación automática del PRIMARY caído a BACKUP.
  - Promoción atómica del BACKUP elegido a PRIMARY con nuevo epoch.
- Processor y Cliente se vuelven a conectar de forma transparente tras el failover (DNS re-resolución).

---

## 5. Nombrado y Localización

- Alias compartido “dns” en la red overlay; Docker DNS retorna múltiples IPs para failover.
- Descubrimiento dinámico:
  - Processor y Storage consultan /dns-servers y /health para elegir DNS saludable.
  - Resolución de Storage activo vía /server/resolve.
  - Resolución de Processor activo vía /processor/resolve (round-robin simple).
- Cache con TTL en clientes para evitar consultas excesivas, invalidada en errores.

---

## 6. Consistencia y Replicación

- Replicación de BD y archivos de PRIMARY a BACKUPs:
  - Snapshot consistente de SQLite vía /internal/db_snapshot.
  - Merge LWW (Last-Modified-Wins) de registros al sincronizar la base:
    - Si hay conflictos, se compara last_modified, write_epoch y version.
    - Preserva versiones locales más nuevas tras particiones.
  - Sincronización de archivos:
    - Descarga de archivos faltantes del PRIMARY.
    - Preservación de divergencias: archivos locales no presentes en PRIMARY se “empujan”.
    - Para discrepancias de contenido, se preservan copias de conflicto.
- Fencing de escrituras:
  - Respuestas con 409/503 por not_primary/lease_expired.
  - Processor invalida cache DNS y reintenta contra el PRIMARY vigente.

---

## 7. Tolerancia a Fallos

- Failover automático de Storage en ~5–15s según timeouts configurados.
- DNS HA con múltiples instancias y alias compartido para re-resolución rápida.
- Circuit breaker en Processor:
  - Abre ante fallos repetidos hacia un Storage específico.
  - Invalida cache DNS y re-resuelve un Storage saludable.
- Cliente:
  - Cachea resoluciones de Processor y valida URLs de descarga.
  - Fallback a BROWSER_API_URL si no hay processors.

---

## 8. Seguridad

- Rate limiting por IP (SlowAPI) en endpoints críticos del Storage.
- CORS centralizado seguro según entorno.
- Logging estructurado de acceso y aplicación, con volumen dedicado.
- HTTPS con Proxy:
  - Terminación TLS y redirección HTTP→HTTPS:
    - Proxy Python sin dependencias externas, ejecutado en contenedor dedicado, con certificados montados en /etc/nginx/certs.
    - Alternativa con Nginx para entornos que permiten esa imagen.
  - Mapeo por SNI/Host hacia servicios internos:
    - client.file-search.local → cliente Streamlit.
    - api.file-search.local → Processor (API pública).
  - Encabezados de proxy agregados: X-Real-IP, X-Forwarded-For, X-Forwarded-Proto=https, preservando Host.
  - Configuración dinámica vía variable de entorno:
    - PROXY_CONFIG con pares dominio=contenedor:puerto; valores por defecto para client_1:8501 y processor_1:8000.
  - Referencias:
    - Implementación Python: [proxy.py](file:///d:/Projects/distributed-systems/file-search%20(posible%20funcional)/proxy.py), [Dockerfile.proxy](file:///d:/Projects/distributed-systems/file-search%20(posible%20funcional)/Dockerfile.proxy)
    - Configuración Nginx: [nginx.conf](file:///d:/Projects/distributed-systems/file-search%20(posible%20funcional)/nginx.conf)
    - Guía de despliegue del proxy TLS: [proxy.md](file:///d:/Projects/distributed-systems/file-search%20(posible%20funcional)/proxy.md)
    - Guías complementarias: [deploy-https-cliente-windows.md](file:///d:/Projects/distributed-systems/file-search%20(posible%20funcional)/deploy-https-cliente-windows.md), [deploy-https-cliente-linux.md](file:///d:/Projects/distributed-systems/file-search%20(posible%20funcional)/deploy-https-cliente-linux.md)
- Próximos pasos recomendados:
  - Autenticación JWT y roles (lectura/descarga vs. administración).
  - Auditoría avanzada y separación de logs de acceso.

---

## 9. Despliegue

- Dockerfiles dedicados por servicio:
  - file-search-dns, file-search-storage, file-search-processor, file-search-client.
- Docker Swarm stack:
  - Red overlay attachable “file_search_net”.
  - Constraints para distribuir PRIMARY/BACKUPs entre nodos manager/worker.
- Variables de entorno controlan puertos, alias DNS, timeouts y rutas de volúmenes.
- Guías operativas en docs para verificación de salud, inspección de red y redeploy.

---

## 10. Puntos de Entrada

- DNS Service: [main.py](file:///d:/Projects/distributed-systems/file-search%20(posible%20funcional)/app/dns_service/main.py)
- Storage Node: [main.py](file:///d:/Projects/distributed-systems/file-search%20(posible%20funcional)/app/storage/main.py)
- Processor Node: [main.py](file:///d:/Projects/distributed-systems/file-search%20(posible%20funcional)/app/processor/main.py)
- Cliente Web: [app.py](file:///d:/Projects/distributed-systems/file-search%20(posible%20funcional)/app/client/app.py)
- Arquitectura y despliegue: [README.md](file:///d:/Projects/distributed-systems/file-search%20(posible%20funcional)/README.md), [stack.separated.yml](file:///d:/Projects/distributed-systems/file-search%20(posible%20funcional)/stack.separated.yml)

---

## 11. Referencias de Código

- DNS HA:
  - Registro y heartbeats de Storage:
    - /server/register, /server/heartbeat, /server/resolve en [main.py](file:///d:/Projects/distributed-systems/file-search%20(posible%20funcional)/app/dns_service/main.py#L476-L592) y [main.py](file:///d:/Projects/distributed-systems/file-search%20(posible%20funcional)/app/dns_service/main.py#L595-L751).
  - Resolución de Processor:
    - /processor/resolve en [main.py](file:///d:/Projects/distributed-systems/file-search%20(posible%20funcional)/app/dns_service/main.py#L990-L1033).
- Storage:
  - Gestión de rol y heartbeats: [node_manager.py](file:///d:/Projects/distributed-systems/file-search%20(posible%20funcional)/app/storage/services/node_manager.py#L28-L96), [node_manager.py](file:///d:/Projects/distributed-systems/file-search%20(posible%20funcional)/app/storage/services/node_manager.py#L180-L199), [node_manager.py](file:///d:/Projects/distributed-systems/file-search%20(posible%20funcional)/app/storage/services/node_manager.py#L266-L345).
  - Sincronización LWW y archivos: [sync_service.py](file:///d:/Projects/distributed-systems/file-search%20(posible%20funcional)/app/storage/services/sync_service.py#L55-L101), [sync_service.py](file:///d:/Projects/distributed-systems/file-search%20(posible%20funcional)/app/storage/services/sync_service.py#L243-L273).
  - Endpoints internos de sync: [endpoints.py](file:///d:/Projects/distributed-systems/file-search%20(posible%20funcional)/app/storage/api/endpoints.py#L484-L528), [endpoints.py](file:///d:/Projects/distributed-systems/file-search%20(posible%20funcional)/app/storage/api/endpoints.py#L530-L633).
- Processor:
  - Cliente de Storage con circuit breaker: [storage_client.py](file:///d:/Projects/distributed-systems/file-search%20(posible%20funcional)/app/processor/services/storage_client.py#L72-L90), [storage_client.py](file:///d:/Projects/distributed-systems/file-search%20(posible%20funcional)/app/processor/services/storage_client.py#L205-L223).
  - API pública del Processor: [endpoints.py](file:///d:/Projects/distributed-systems/file-search%20(posible%20funcional)/app/processor/api/endpoints.py#L1-L15), [endpoints.py](file:///d:/Projects/distributed-systems/file-search%20(posible%20funcional)/app/processor/api/endpoints.py#L141-L147).
- Cliente:
  - Resolución de Processor y fallback: [app.py](file:///d:/Projects/distributed-systems/file-search%20(posible%20funcional)/app/client/app.py#L110-L147), [app.py](file:///d:/Projects/distributed-systems/file-search%20(posible%20funcional)/app/client/app.py#L217-L245).
- Documentación base:
  - Informe anterior y guías: [docs/INFORME_2DA_ENTREGA.md](file:///d:/Projects/distributed-systems/file-search%20(posible%20funcional)/docs/INFORME_2DA_ENTREGA.md), [docs/DEPLOYMENT_GUIDE.md](file:///d:/Projects/distributed-systems/file-search%20(posible%20funcional)/docs/DEPLOYMENT_GUIDE.md), [docs/ARCHITECTURE_SEPARATED.md](file:///d:/Projects/distributed-systems/file-search%20(posible%20funcional)/docs/ARCHITECTURE_SEPARATED.md)

---

Conclusión:

- El sistema evoluciona hacia una arquitectura separada Processor–Storage con DNS HA que mejora disponibilidad y escalabilidad.
- La replicación LWW y el fencing mitigan conflictos tras particiones y evitan escrituras en nodos no válidos.
- El failover es transparente para clientes gracias a la re-resolución vía DNS y circuit breaker.

---

## 12. Conceptos

- Alta Disponibilidad (HA): diseño para mantener el servicio operativo ante fallos, usando redundancia y failover.
- PRIMARY–BACKUP: patrón donde un nodo PRIMARY atiende operaciones y uno o más BACKUPs mantienen copias para asumir ante fallos.
- Failover automático: promoción de un BACKUP a PRIMARY cuando el PRIMARY no responde dentro de un timeout.
- Epoch y Lease (Fencing): epoch monotónico y lease con expiración para impedir escrituras en nodos desautorizados; respuestas 409/503 señalan not_primary/lease_expired.
- Split-brain: condición con múltiples nodos creyendo ser PRIMARY; se mitiga degradando nodos y promoviendo uno solo.
- DNS HA y Descubrimiento: múltiples instancias DNS bajo alias “dns”; clientes consultan /health, /dns-servers y resuelven Storage/Processor con failover.
- Red overlay: red distribuida de Docker Swarm que permite comunicación entre servicios en distintos hosts.
- Processor stateless: capa sin estado persistente que enruta y reintenta, escalable horizontalmente sin rol PRIMARY/BACKUP.
- Circuit breaker: mecanismo que corta llamadas a un destino fallando repetidamente e invalida cachés para re-resolver.
- Round-robin: selección simple de Processor activo para distribuir carga.
- Proxy: componente intermediario que recibe peticiones del cliente y las reenvía al servicio interno adecuado. Permite:
  - Terminación TLS, balanceo y reescritura de encabezados.
  - Aislar servicios internos y exponer un único punto de entrada seguro.
- TLS (Transport Layer Security): protocolo criptográfico que cifra y autentica la comunicación entre cliente y servidor mediante certificados X.509.
- HTTPS: HTTP sobre TLS; garantiza confidencialidad, integridad y autenticidad del tráfico.
- SNI (Server Name Indication): extensión de TLS que permite seleccionar certificados según el hostname solicitado en la misma IP/puerto.
- Proxy HTTPS/TLS y SNI: terminación TLS con certificados, redirección HTTP→HTTPS y mapeo por Host a servicios internos usando SNI.
- Rate limiting y CORS: control de abuso por IP y políticas de origen seguro centralizadas.
- Logging estructurado: separación de access logs y application logs en volumen dedicado.
- LWW (Last-Modified-Wins): estrategia de resolución de conflictos tomando la versión con mayor last_modified, con respaldos por contenido divergente.
- Fencing de escrituras: rechazo de operaciones de escritura cuando el nodo no es PRIMARY o el lease expiró.

---

## 13. PRIMARY–BACKUP y Failover por capa

- Storage:
  - Arranque: el nodo descubre un DNS saludable, se registra y recibe rol (PRIMARY o BACKUP).
  - Si es PRIMARY: atiende lecturas/escrituras; el DNS emite lease con expiración y epoch; renueva lease en heartbeats; fencing bloquea escrituras si el lease expira.
  - Si es BACKUP: configura la URL del PRIMARY, ejecuta un “full sync” inicial y luego un loop de sincronización:
    - Base de datos: merge LWW preservando versiones locales más nuevas tras particiones.
    - Archivos: descarga faltantes del PRIMARY, preserva divergencias locales y marca conflictos por contenido.
  - Heartbeats: el DNS mantiene tiempos de vida; si el PRIMARY excede el timeout, lo degrada y promueve el BACKUP más antiguo vivo, incrementa epoch y asigna nuevo lease.
  - Reincorporación: un nodo que vuelve se registra como BACKUP, se sincroniza y puede ser promovido si el PRIMARY cae.

- DNS:
  - Descubrimiento y salud: múltiples instancias bajo alias “dns”; clientes consultan /health y /dns-servers.
  - Registro: /server/register asigna rol; si no hay PRIMARY vivo, el primero registrado es PRIMARY; si ya hay PRIMARY, nuevos nodos son BACKUP.
  - Heartbeats: /server/heartbeat valida tiempos; si el PRIMARY cae, ejecuta failover automático promoviendo un BACKUP vivo con mayor antigüedad; propaga estado a otros DNS.
  - Resolución: /server/resolve devuelve el PRIMARY vigente; si no hay PRIMARY vivo, promueve en tiempo de resolución y retorna el nuevo PRIMARY.
  - Fencing: respuestas incluyen epoch y lease; clientes y Processor respetan errores 409/503 para evitar escrituras no autorizadas.

- Processor (por qué no PRIMARY–BACKUP):
  - Es una capa sin estado: no mantiene BD ni archivos ni sincronización; su función es enrutar peticiones al Storage activo.
  - La alta disponibilidad se obtiene por tener múltiples instancias y por resolución DNS; no requiere coordinación de roles.
  - Ante fallos del Storage, usa circuit breaker para invalidar cache y re-resolver el PRIMARY vigente; el failover es transparente para el cliente.








Qué se explica ahora

- Proxy: componente intermediario que recibe las peticiones de los clientes y las reenvía a los servicios internos, realizando terminación TLS, aplicando encabezados, y exponiendo un único punto de entrada seguro.
- TLS: protocolo criptográfico que cifra y autentica la conexión mediante certificados X.509; HTTPS es HTTP sobre TLS, garantizando confidencialidad, integridad y autenticidad.
- SNI: extensión de TLS para seleccionar certificados según el hostname cuando se comparte IP/puerto.
- Storage:
  - Arranque y registro con DNS; asignación de rol; lease y epoch cuando es PRIMARY.
  - BACKUP realiza full sync inicial y sincronización continua, con LWW en BD y preservación de divergencias en archivos.
  - Heartbeats y failover: promoción del BACKUP más antiguo vivo cuando el PRIMARY cae; epoch++ y nuevo lease; callbacks en Storage ajustan comportamiento.
- DNS:
  - Descubrimiento y salud; registro de Storage; decisiones de rol; failover automático en heartbeat y en resolve; fencing mediante epoch/lease.
- Processor:
  - Capa sin estado orientada a enrutar; alta disponibilidad por multiplicidad de instancias y resolución DNS; circuit breaker para re-resolver tras fallos; no requiere coordinación de roles.