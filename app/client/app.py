"""Interfaz Streamlit para el buscador de archivos."""

from __future__ import annotations

import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Dict, List
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
    )
except ModuleNotFoundError:
    from ui_components import (
        pagination_controls,
        render_results,
        search_form,
        setup_page,
        show_error,
        show_warning,
    )

# --- Configuración de Servicio ---
DNS_SERVICE_HOST = os.getenv("DNS_SERVICE_HOST", "dns_service")
TARGET_SERVICE_NAME = os.getenv("TARGET_SERVICE_NAME", "server")
TARGET_SERVICE_PORT = os.getenv("TARGET_SERVICE_PORT", "8000")
DEFAULT_PAGE_SIZE = int(os.getenv("CLIENT_PAGE_SIZE", "10"))
DEFAULT_TYPE = "Todos"

# Inicializar el resolvedor DNS una sola vez
resolver = None
if DNSClient:
    try:
        resolver = DNSClient(dns_host=DNS_SERVICE_HOST)
        logger.info("DNSClient inicializado correctamente.")
    except Exception as e:
        logger.error(f"Error inicializando DNSClient: {e}")

def get_api_base_url() -> str:
    """
    Obtiene la URL base del servidor API.
    Intenta resolver la IP dinámicamente usando el servicio DNS personalizado.
    """
    if resolver:
        try:
            # Paso clave: Preguntar al DNS Service dónde está el 'server'
            server_ip = resolver.resolve(TARGET_SERVICE_NAME)
            return f"http://{server_ip}:{TARGET_SERVICE_PORT}"
        except Exception as e:
            logger.error(f"Fallo en resolución DNS para '{TARGET_SERVICE_NAME}': {e}")
    
    # Fallback: Usar variable de entorno o localhost si falla el DNS
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


@st.cache_data(ttl=10)
def fetch_files(query: str, *, limit: int, offset: int) -> List[Dict]:
    target_url = api_url("search")
    logger.info(f"Realizando petición a: {target_url}")
    
    params = {"query": query, "limit": limit, "offset": offset}
    response = requests.get(target_url, params=params, timeout=15)
    response.raise_for_status()
    return response.json()


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
