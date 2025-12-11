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

# Configuración de reintentos
MAX_RETRIES = int(os.getenv("MAX_RETRIES", 3))
RETRY_DELAY = float(os.getenv("RETRY_DELAY", 0.5))

# Cache de la URL del servidor
_cached_server_url: Optional[str] = None
_cache_timestamp: float = 0
_cache_ttl: float = 30  # TTL del cache en segundos


def _discover_dns_url() -> Optional[str]:
    """Descubre la URL de un servidor DNS usando el alias de Docker."""
    import socket
    try:
        results = socket.getaddrinfo(DNS_ALIAS, DNS_SERVICE_PORT, socket.AF_INET, socket.SOCK_STREAM)
        ips = sorted(set(result[4][0] for result in results))  # Ordenar para consistencia
        if ips:
            return f"http://{ips[0]}:{DNS_SERVICE_PORT}"
    except Exception as e:
        logger.warning(f"Error descubriendo DNS: {e}")
    return None


def _resolve_server_from_dns() -> Optional[str]:
    """
    Pregunta al DNS por el servidor API PRIMARY actual.
    Usa el endpoint /server/resolve.
    """
    global _cached_server_url, _cache_timestamp
    
    # Verificar cache
    if _cached_server_url and (time.time() - _cache_timestamp) < _cache_ttl:
        return _cached_server_url
    
    dns_url = _discover_dns_url()
    if not dns_url:
        logger.warning("No se pudo descubrir el DNS")
        return None
    
    try:
        response = requests.get(f"{dns_url}/server/resolve", timeout=5)
        if response.status_code == 200:
            data = response.json()
            server_url = data.get("url")
            if server_url:
                _cached_server_url = server_url
                _cache_timestamp = time.time()
                logger.info(f"Servidor resuelto via DNS: {server_url}")
                return server_url
        elif response.status_code == 503:
            logger.warning("DNS reporta que no hay servidores disponibles")
    except Exception as e:
        logger.error(f"Error consultando DNS: {e}")
    
    return None


def _invalidate_server_cache():
    """Invalida el cache del servidor para forzar re-resolución."""
    global _cached_server_url, _cache_timestamp
    _cached_server_url = None
    _cache_timestamp = 0
    logger.info("Cache de servidor invalidado")


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
    fallback = os.getenv("API_BASE_URL", "http://localhost:8000")
    logger.warning(f"Usando URL de fallback: {fallback}")
    return fallback

def api_url(path: str) -> str:
    """Construye una URL para la API interna usando la IP resuelta."""
    base_url = get_api_base_url()
    return urljoin(base_url.rstrip("/") + "/", path.lstrip("/"))

def build_download_url(record: Dict) -> str:
    """Construye la URL de descarga para el navegador del usuario."""
    # NOTA: El navegador NO usa nuestro DNS interno, así que seguimos usando
    # la URL pública/externa configurada.
    browser_url = os.getenv("BROWSER_API_URL", "http://localhost:8000")
    return urljoin(browser_url.rstrip("/") + "/", f"files/{record['file_id']}/download")


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
        data = {}
        
        if folder:
            data["folder"] = folder
        
        # Enviar con reintentos
        response = make_request_with_retry("POST", "upload", files=files, data=data)
        return response.json()
    
    except Exception as e:
        logger.error(f"Error al subir archivo: {e}")
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
    st.session_state.setdefault("limit", DEFAULT_PAGE_SIZE)
    st.session_state.setdefault("page", 0)
    st.session_state.setdefault("available_types", [DEFAULT_TYPE])


def on_search_submit():
    """Callback que se ejecuta cuando se envía el formulario de búsqueda."""
    st.session_state.query = st.session_state.search_query.strip()
    st.session_state.file_type = st.session_state.search_type
    st.session_state.limit = st.session_state.search_limit
    st.session_state.page = 0

def main() -> None:
    ensure_state_defaults()
    setup_page()
    
    # ========== Formulario de subida ==========
    uploaded_file, folder, upload_submitted = upload_form()
    
    if upload_submitted and uploaded_file:
        with st.spinner(f"Subiendo {uploaded_file.name}..."):
            try:
                result = upload_file_to_server(uploaded_file, folder)
                st.success(
                    f"✅ Archivo '{result['filename']}' subido correctamente "
                    f"({result['size']} bytes)"
                )
                # Limpiar cache para que aparezca en búsquedas
                fetch_files.clear()
                st.rerun()
            except Exception as e:
                st.error(f"❌ Error al subir archivo: {str(e)}")
    # ==========================================
    
    if "search_query" not in st.session_state:
        st.session_state.search_query = st.session_state.query
    if "search_type" not in st.session_state:
        st.session_state.search_type = st.session_state.file_type
    if "search_limit" not in st.session_state:
        st.session_state.search_limit = st.session_state.limit

    query, type_choice, limit_choice, submitted = search_form(
        default_query=st.session_state.search_query,
        file_types=st.session_state.available_types,
        default_file_type=st.session_state.search_type,
        default_limit=st.session_state.search_limit,
        on_submit=on_search_submit,
    )

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
