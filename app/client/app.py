"""Interfaz Streamlit para el buscador de archivos."""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import urljoin

import importlib
import requests
from requests import RequestException

# --- Configuración de Logs ---
# Configuramos el logging básico para ver la salida en la consola de Docker
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s :: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S%z"
)
logger = logging.getLogger("client.app")

# --- Importación de DNSClient ---
try:
    from common.resolver import DNSClient
except ImportError:
    logger.warning("No se pudo importar DNSClient. Verifica el PYTHONPATH.")
    DNSClient = None

try:
    streamlit = importlib.import_module("streamlit")
except ModuleNotFoundError as exc:
    raise SystemExit(
        "Streamlit no está instalado. Asegúrate de ejecutar `pip install streamlit requests`."
    ) from exc

st = streamlit

try:
    from client.ui_components import (
        pagination_controls,
        render_results,
        search_form,
        setup_page,
        show_error,
        show_warning,
        upload_form,
    )
except ModuleNotFoundError:
    from ui_components import (
        pagination_controls,
        render_results,
        search_form,
        setup_page,
        show_error,
        show_warning,
        upload_form,
    )

# --- Configuración de Servicio ---
DNS_ALIAS = os.getenv("DNS_ALIAS", "dns")
DNS_SERVICE_PORT = int(os.getenv("DNS_SERVICE_PORT", 5353))
TARGET_SERVICE_NAME = os.getenv("TARGET_SERVICE_NAME", "server")
TARGET_SERVICE_PORT = os.getenv("TARGET_SERVICE_PORT", "8000")
DEFAULT_PAGE_SIZE = int(os.getenv("CLIENT_PAGE_SIZE", "10"))
DEFAULT_TYPE = "Todos"

# URL base para el navegador del usuario (fuera de Docker)
# Esta URL es la que el navegador usará para descargar archivos
BROWSER_API_URL = os.getenv("BROWSER_API_URL", "http://localhost:8000")

# Configuración de reintentos
MAX_RETRIES = int(os.getenv("MAX_RETRIES", 3))
RETRY_DELAY = float(os.getenv("RETRY_DELAY", 0.5))

# Cache de la URL del servidor
_cached_server_url: Optional[str] = None
_cache_timestamp: float = 0
_cache_ttl: float = 30  # TTL del cache en segundos

# Cache de processors disponibles (para URLs de descarga)
_cached_processors: List[Dict] = []
_processors_cache_timestamp: float = 0
_processors_cache_ttl: float = 15  # TTL del cache de processors en segundos

# Cache de URLs de descarga validadas por processor
# {processor_id: {"url": str, "timestamp": float, "is_localhost": bool}}
_validated_download_urls: Dict[str, Dict] = {}
_download_url_cache_ttl: float = 60  # TTL del cache de URLs validadas (1 minuto)

# Timeout para validación de URLs (debe ser corto para no bloquear la UI)
URL_VALIDATION_TIMEOUT: float = float(os.getenv("URL_VALIDATION_TIMEOUT", 2.0))


def _get_protocol() -> str:
    """
    Retorna el protocolo (http o https) basado en BROWSER_API_URL o headers.
    """
    try:
        ctx = getattr(st, "context", None)
        if ctx and hasattr(ctx, "headers"):
            # 1. Check X-Forwarded-Proto
            proto = ctx.headers.get("x-forwarded-proto", "").lower()
            if proto == "https":
                return "https"
            # 2. Check referer
            referer = ctx.headers.get("referer", "")
            if referer.startswith("https"):
                return "https"
            # 3. Check origin
            origin = ctx.headers.get("origin", "")
            if origin.startswith("https"):
                return "https"
    except Exception:
        pass

    if BROWSER_API_URL.startswith("https") or ".local" in BROWSER_API_URL:
        return "https"
    return "http"


def _discover_dns_urls() -> List[str]:
    """Descubre todas las URLs de DNS usando el alias de Docker (puede devolver varias IPs)."""
    import socket
    protocol = "http" # El DNS siempre es interno en HTTP en este proyecto
    try:
        results = socket.getaddrinfo(DNS_ALIAS, DNS_SERVICE_PORT, socket.AF_INET, socket.SOCK_STREAM)
        ips = sorted(set(result[4][0] for result in results))  # Ordenar para consistencia
        return [f"{protocol}://{ip}:{DNS_SERVICE_PORT}" for ip in ips]
    except Exception as e:
        logger.warning(f"Error descubriendo DNS: {e}")
    return []


def _resolve_server_from_dns() -> Optional[str]:
    """
    Pregunta al DNS por un Processor Node disponible.
    Usa el endpoint /processor/resolve.
    
    Si BROWSER_API_URL apunta al proxy (api.file-search.local), 
    se prioriza el uso del proxy para mantener la alta disponibilidad
    aunque un nodo individual falle.
    """
    global _cached_server_url, _cache_timestamp
    
    # Prioridad: Si el cliente está configurado para usar el proxy, usarlo.
    if "api.file-search.local" in BROWSER_API_URL:
        # En la red interna de Docker, el proxy se llama 'proxy' o 'processor' (alias)
        # Pero aquí devolvemos el alias de red 'processor' para que el cliente
        # dentro de Docker hable con el balanceador de carga de Docker.
        return f"http://processor:8000"

    # Verificar cache
    if _cached_server_url and (time.time() - _cache_timestamp) < _cache_ttl:
        return _cached_server_url
    
    dns_urls = _discover_dns_urls()
    if not dns_urls:
        logger.warning("No se pudo descubrir ningún DNS")
        return None

    last_error: Optional[Exception] = None
    for dns_url in dns_urls:
        try:
            response = requests.get(f"{dns_url}/processor/resolve", timeout=5)
            if response.status_code == 200:
                data = response.json()
                server_url = data.get("url")
                if server_url:
                    _cached_server_url = server_url
                    _cache_timestamp = time.time()
                    logger.info(f"Processor resuelto via DNS ({dns_url}): {server_url}")
                    return server_url
            elif response.status_code == 503:
                logger.warning(f"{dns_url} reporta que no hay processors disponibles")
        except Exception as e:
            last_error = e
            logger.warning(f"Error consultando DNS {dns_url}: {e}")

    if last_error:
        logger.error(f"No se pudo consultar ningún DNS (último error: {last_error})")
    
    return None


def _invalidate_server_cache():
    """Invalida el cache del servidor y URLs de descarga para forzar re-resolución."""
    global _cached_server_url, _cache_timestamp, _validated_download_urls
    _cached_server_url = None
    _cache_timestamp = 0
    _validated_download_urls.clear()  # También invalidar URLs de descarga
    logger.info("Cache de servidor y URLs de descarga invalidados")


def _get_available_processors() -> List[Dict]:
    """
    Obtiene la lista de processors disponibles desde el DNS.
    
    Returns:
        Lista de diccionarios con información de processors disponibles.
        Cada diccionario contiene: processor_id, ip, port, url, alive
    """
    global _cached_processors, _processors_cache_timestamp
    
    # Verificar cache
    if _cached_processors and (time.time() - _processors_cache_timestamp) < _processors_cache_ttl:
        return _cached_processors
    
    # Obtener lista de processors desde el DNS
    dns_urls = _discover_dns_urls()
    if not dns_urls:
        logger.warning("No se pudo descubrir ningún DNS para obtener processors")
        return []
    
    for dns_url in dns_urls:
        try:
            response = requests.get(f"{dns_url}/processor/list", timeout=5)
            if response.status_code == 200:
                data = response.json()
                processors = data.get("processors", [])
                # Filtrar solo los processors activos
                active_processors = [p for p in processors if p.get("alive", False)]
                
                if active_processors:
                    _cached_processors = active_processors
                    _processors_cache_timestamp = time.time()
                    logger.info(f"Processors disponibles obtenidos desde DNS: {len(active_processors)}")
                    return active_processors
        except Exception as e:
            logger.warning(f"Error obteniendo processors desde DNS {dns_url}: {e}")
    
    logger.warning("No se pudo obtener la lista de processors desde ningún DNS")
    return []


def _map_processor_to_browser_url(processor_ip: str, processor_port: int) -> str:
    """
    Mapea la IP interna de un processor a una URL accesible desde el navegador.
    
    DEPRECATED: Esta función ya no es necesaria ya que el DNS devuelve external_url.
    Se mantiene para compatibilidad hacia atrás.
    
    Args:
        processor_ip: IP interna del processor en la red Docker
        processor_port: Puerto interno del processor
    
    Returns:
        URL accesible desde el navegador (localhost:PUERTO_EXPUESTO)
    """
    return f"{_get_protocol()}://localhost:{processor_port}"


def _get_processor_download_url() -> Optional[str]:
    """
    Obtiene una URL de processor disponible para descargas.
    
    Usa round-robin simple entre los processors disponibles.
    
    Returns:
        URL base del processor para usar en descargas, o None si no hay disponibles
    """
    processors = _get_available_processors()
    if not processors:
        # Fallback a BROWSER_API_URL si no podemos obtener processors del DNS
        logger.warning("No hay processors disponibles, usando BROWSER_API_URL como fallback")
        return BROWSER_API_URL
    
    # Round-robin simple: rotar entre processors
    # Usar selección aleatoria para distribuir la carga
    import random
    processor = random.choice(processors)
    
    # Usar external_url si está disponible, sino construir URL
    processor_url = processor.get("external_url")
    if not processor_url:
        # Fallback: construir URL usando el puerto externo o interno
        external_port = processor.get("external_port", processor.get("port", 8000))
        processor_url = f"{_get_protocol()}://localhost:{external_port}"
    
    logger.debug(f"Processor seleccionado para descarga: {processor['processor_id']} -> {processor_url}")
    return processor_url


def get_api_base_url() -> str:
    """
    Obtiene la URL base del servidor API.
    Primero intenta resolver via el DNS, luego usa fallback.
    """
    # Intentar resolver via DNS
    server_url = _resolve_server_from_dns()
    if server_url:
        return server_url
    
    # Fallback: Usar variable de entorno o localhost
    fallback = os.getenv("API_BASE_URL", f"{_get_protocol()}://localhost:8000")
    logger.warning(f"Usando URL de fallback: {fallback}")
    return fallback

def api_url(path: str) -> str:
    """Construye una URL para la API interna usando la IP resuelta."""
    base_url = get_api_base_url()
    return urljoin(base_url.rstrip("/") + "/", path.lstrip("/"))


def _validate_url_reachable(url: str, timeout: float = None) -> bool:
    """
    Valida si una URL es alcanzable haciendo una petición HEAD rápida.
    
    Args:
        url: URL base del processor (sin el path del archivo)
        timeout: Timeout en segundos para la validación
    
    Returns:
        True si la URL es alcanzable, False en caso contrario
    """
    if timeout is None:
        timeout = URL_VALIDATION_TIMEOUT
    
    try:
        # Usar HEAD al endpoint /health para validación rápida
        health_url = f"{url.rstrip('/')}/health"
        response = requests.head(health_url, timeout=timeout, allow_redirects=True)
        return response.status_code < 500  # 2xx, 3xx, 4xx son "alcanzables"
    except (requests.ConnectionError, requests.Timeout) as e:
        logger.debug(f"URL no alcanzable {url}: {e}")
        return False
    except Exception as e:
        logger.warning(f"Error inesperado validando URL {url}: {e}")
        return False


def _get_validated_processor_url(processor: Dict) -> str:
    """
    Obtiene una URL validada para un processor, con fallback a localhost.
    
    Primero intenta la URL externa (IP del host), si no es alcanzable
    usa localhost como fallback. Los resultados se cachean para evitar
    validaciones repetidas.
    
    Args:
        processor: Diccionario con información del processor
    
    Returns:
        URL base validada del processor
    """
    global _validated_download_urls
    
    processor_id = processor.get("processor_id", "unknown")
    external_port = processor.get("external_port", processor.get("port", 8000))
    
    # Verificar cache
    cached = _validated_download_urls.get(processor_id)
    if cached and (time.time() - cached["timestamp"]) < _download_url_cache_ttl:
        logger.debug(f"Usando URL cacheada para {processor_id}: {cached['url']}")
        return cached["url"]
    
    # Construir URLs candidatas
    protocol = _get_protocol()
    primary_url = processor.get("external_url")
    if primary_url:
        # Asegurar que el protocolo coincida con el deseado si es una URL del proxy
        if ".local" in primary_url and not primary_url.startswith(protocol):
            primary_url = primary_url.replace("http://", "https://")
    else:
        external_ip = processor.get("external_ip", "localhost")
        primary_url = f"{protocol}://{external_ip}:{external_port}"
    
    localhost_url = f"{protocol}://localhost:{external_port}"
    
    # Si la URL primaria ya es localhost, no hay necesidad de validar
    if "localhost" in primary_url or "127.0.0.1" in primary_url:
        _validated_download_urls[processor_id] = {
            "url": primary_url,
            "timestamp": time.time(),
            "is_localhost": True
        }
        return primary_url
    
    # Validar la URL primaria (IP externa)
    logger.debug(f"Validando URL primaria para {processor_id}: {primary_url}")
    if _validate_url_reachable(primary_url):
        logger.info(f"URL primaria validada para {processor_id}: {primary_url}")
        _validated_download_urls[processor_id] = {
            "url": primary_url,
            "timestamp": time.time(),
            "is_localhost": False
        }
        return primary_url
    
    # Fallback a localhost
    logger.warning(
        f"URL primaria no alcanzable para {processor_id} ({primary_url}), "
        f"usando fallback localhost: {localhost_url}"
    )
    _validated_download_urls[processor_id] = {
        "url": localhost_url,
        "timestamp": time.time(),
        "is_localhost": True
    }
    return localhost_url


def _invalidate_download_url_cache(processor_id: str = None):
    """
    Invalida el cache de URLs de descarga.
    
    Args:
        processor_id: ID del processor a invalidar, o None para invalidar todo
    """
    global _validated_download_urls
    
    if processor_id:
        _validated_download_urls.pop(processor_id, None)
        logger.debug(f"Cache de URL de descarga invalidado para {processor_id}")
    else:
        _validated_download_urls.clear()
        logger.debug("Cache de URLs de descarga invalidado completamente")


def build_download_url(record: dict) -> str:
    """
    Construye la URL de descarga para un archivo con validación automática.
    """
    file_id = record.get("file_id", "")
    if not file_id:
        return "#"
    
    # Determinar protocolo actual de la página
    current_proto = _get_protocol()
    force_https = (current_proto == "https")
    
    # Detectar si estamos accediendo via dominio .local
    try:
        ctx = getattr(st, "context", None)
        if ctx and hasattr(ctx, "headers"):
            current_host = ctx.headers.get("host", "")
            if ".local" in current_host:
                force_https = True
    except Exception:
        pass

    # Si la página está en HTTPS, la descarga DEBE ser HTTPS
    if force_https:
        # Si BROWSER_API_URL es una IP, no servirá para HTTPS (error de certificado)
        # Debemos usar el dominio del proxy que sí tiene el certificado
        url = BROWSER_API_URL.rstrip('/')
        if not any(d in url for d in ["api.file-search.local", "client.file-search.local"]) or url.startswith("http://"):
             # Forzar dominio seguro del proxy
             url = "https://api.file-search.local"
        
        return f"{url}/files/{file_id}/download"
    
    # Fallback a processors individuales si estamos en HTTP (desarrollo local)
    processors = _get_available_processors()
    
    if not processors:
        # Fallback total: usar BROWSER_API_URL
        logger.warning("No hay processors disponibles, usando BROWSER_API_URL como fallback")
        return f"{BROWSER_API_URL.rstrip('/')}/files/{file_id}/download"
    
    # Seleccionar processor (round-robin simple con selección aleatoria)
    import random
    processor = random.choice(processors)
    
    # Obtener URL validada (con fallback automático a localhost si es necesario)
    validated_url = _get_validated_processor_url(processor)
    
    return f"{validated_url.rstrip('/')}/files/{file_id}/download"






def make_request_with_retry(method: str, path: str, **kwargs) -> requests.Response:
    """
    Realiza una petición HTTP con reintentos y re-resolución de DNS en caso de fallo.
    
    Si la petición falla (conexión rechazada, timeout, etc.), invalida el cache
    del servidor y reintenta con una nueva resolución DNS.
    """
    last_exception = None
    
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            url = api_url(path)
            logger.debug(f"Intento {attempt}: {method} {url}")
            
            # Mayor timeout para POST con archivos
            timeout = 60 if method.upper() == "POST" and "files" in kwargs else 15
            
            if method.upper() == "GET":
                response = requests.get(url, timeout=timeout, **kwargs)
            elif method.upper() == "POST":
                response = requests.post(url, timeout=timeout, **kwargs)
            else:
                raise ValueError(f"Método no soportado: {method}")
            
            response.raise_for_status()
            return response
            
        except (requests.ConnectionError, requests.Timeout) as e:
            logger.warning(f"Intento {attempt} falló: {e}")
            last_exception = e
            
            # Invalidar cache y forzar re-resolución en el siguiente intento
            _invalidate_server_cache()
            
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY)
        except requests.HTTPError as e:
            # Errores HTTP (4xx, 5xx) no se reintentan
            raise
    
    # Agotados los reintentos
    logger.error(f"Todos los reintentos fallaron. Última excepción: {last_exception}")
    raise last_exception


@st.cache_data(ttl=10)
def fetch_files(query: str, *, limit: int, offset: int) -> List[Dict]:
    params = {"query": query, "limit": limit, "offset": offset}
    response = make_request_with_retry("GET", "search", params=params)
    data = response.json()
    return data.get("results", data) if isinstance(data, dict) else data


def upload_file_to_server(uploaded_file, folder: str = "") -> dict:
    """
    Sube un archivo al servidor usando el endpoint /upload.
    
    Args:
        uploaded_file: Archivo desde st.file_uploader
        folder: Carpeta destino opcional
    
    Returns:
        Respuesta JSON del servidor
    """
    try:
        # Preparar el archivo para enviarlo
        files = {"file": (uploaded_file.name, uploaded_file.getvalue(), uploaded_file.type)}

        # Enviar con reintentos (sin carpeta destino: la UI ya no la solicita)
        response = make_request_with_retry("POST", "upload", files=files, data={})
        return response.json()

    except Exception as e:
        logger.error(f"Error al subir archivo: {e}")
        raise


def download_file_from_server(file_id: str, file_name: str) -> bytes:
    """
    Descarga un archivo del servidor usando el endpoint /files/{file_id}/download.
    
    Args:
        file_id: ID del archivo a descargar
        file_name: Nombre del archivo (para logging)
    
    Returns:
        Contenido del archivo en bytes
    """
    try:
        logger.info(f"Descargando archivo: {file_name} (ID: {file_id})")
        response = make_request_with_retry("GET", f"files/{file_id}/download")
        return response.content
    
    except Exception as e:
        logger.error(f"Error al descargar archivo {file_name}: {e}")
        raise


def humanize_datetime(value: str) -> str:
    try:
        value = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(value)
        return dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return value


def extract_types(records: List[Dict]) -> List[str]:
    extensions = {
        Path(record["name"]).suffix.lower() or "Sin extensión" for record in records
    }
    return [DEFAULT_TYPE] + sorted(extensions)


def matches_type(record: Dict, selected: str) -> bool:
    if selected == DEFAULT_TYPE:
        return True
    suffix = Path(record["name"]).suffix.lower() or "Sin extensión"
    return suffix == selected


def ensure_state_defaults() -> None:
    st.session_state.setdefault("query", "")
    st.session_state.setdefault("file_type", DEFAULT_TYPE)
    # Forzar 10 resultados por página según requisito
    st.session_state.setdefault("limit", 10)
    st.session_state.setdefault("page", 0)
    st.session_state.setdefault("available_types", [DEFAULT_TYPE])


def on_search_submit():
    """Callback que se ejecuta cuando se envía el formulario de búsqueda."""
    st.session_state.query = st.session_state.search_query.strip()
    # No hay selectores de tipo/limit en la nueva UI; conservar valores por defecto
    st.session_state.page = 0

def main() -> None:
    ensure_state_defaults()
    setup_page()
    # Crear dos columnas: izquierda -> búsqueda (solo text-input), derecha -> subida
    col_left, col_right = st.columns([2, 1])

    # Inicializar estado del campo de búsqueda si es necesario
    if "search_query" not in st.session_state:
        st.session_state.search_query = st.session_state.query

    # Renderizar formularios en sus columnas
    with col_left:
        query, submitted = search_form(
            default_query=st.session_state.search_query,
            on_submit=on_search_submit,
        )

    with col_right:
        uploaded_file, upload_submitted = upload_form()

    # Manejar subida
    if upload_submitted and uploaded_file:
        with st.spinner(f"Subiendo {uploaded_file.name}..."):
            try:
                result = upload_file_to_server(uploaded_file)
                st.success(
                    f"✅ Archivo '{result.get('filename', uploaded_file.name)}' subido correctamente "
                    f"({result.get('size', '?')} bytes)"
                )
                # Limpiar cache para que aparezca en búsquedas
                fetch_files.clear()
                st.rerun()
            except Exception as e:
                st.error(f"❌ Error al subir archivo: {str(e)}")

    if not st.session_state.query:
        show_warning("Introduce un término de búsqueda para comenzar.")
        return

    page = st.session_state.page
    limit = st.session_state.limit
    offset = page * limit

    try:
        raw_results = fetch_files(
            st.session_state.query,
            limit=limit + 1,
            offset=offset,
        )
    except RequestException as exc:
        show_error(f"No se pudo conectar con la API: {exc}")
        return

    st.session_state.available_types = extract_types(raw_results)

    has_next = len(raw_results) > limit
    display_records = raw_results[:limit]
    filtered_records = [
        {
            **record,
            "last_modified": humanize_datetime(record.get("last_modified", "")),
        }
        for record in display_records
        if matches_type(record, st.session_state.file_type)
    ]

    render_results(
        filtered_records,
        build_download_url=build_download_url,
        timezone_label="local",
    )

    action = pagination_controls(
        page=page,
        has_previous=page > 0,
        has_next=has_next,
    )

    if action == "prev":
        st.session_state.page -= 1
        st.rerun()
    elif action == "next":
        st.session_state.page += 1
        st.rerun()


if __name__ == "__main__":
    main()
