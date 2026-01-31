"""Componentes reutilizables para la interfaz de Streamlit."""

from __future__ import annotations

import importlib
from typing import Callable, Iterable, List, Optional

try:
	streamlit = importlib.import_module("streamlit")
except ModuleNotFoundError as exc:  # pragma: no cover - para entornos sin dependencias
	raise SystemExit(
		"Streamlit no está instalado. Asegúrate de instalar las dependencias del cliente."
	) from exc

st = streamlit


def setup_page() -> None:
	"""Configura la página principal y estilo base."""

	st.set_page_config(page_title="File Search", layout="wide")
	st.title("📁 Buscador de archivos")
	st.caption(
		"Busca y descarga los documentos disponibles en el repositorio compartido."
	)


def search_form(
	*,
	default_query: str,
	on_submit=None,
) -> tuple[str, bool]:
	"""Renderiza un formulario de búsqueda simple (solo text-input).

	Devuelve la query y si se pulsó buscar.
	"""

	with st.form("search_form", clear_on_submit=False):
		st.text_input(
			"Buscar por nombre o ruta",
			value=default_query,
			placeholder="Ej. reporte",
			key="search_query"
		)
		submitted = st.form_submit_button("Buscar", on_click=on_submit if on_submit else None)

	query = st.session_state.get("search_query", default_query)
	return query, submitted


def upload_form() -> tuple[Optional[object], bool]:
	"""Renderiza el formulario de subida de archivos simplificado.

	Solo muestra un file_uploader y el botón de subir. No solicita carpeta destino.
	Devuelve (uploaded_file, submitted).
	"""

	with st.form("upload_form", clear_on_submit=True):
		uploaded_file = st.file_uploader(
			"Selecciona un archivo",
			type=None,  # Permite cualquier tipo de archivo
			help="Sube un archivo al repositorio compartido"
		)
		submitted = st.form_submit_button("Subir archivo", type="primary")

		if submitted and uploaded_file is not None:
			return uploaded_file, True

	return None, False


def render_results(
	records: List[dict],
	*,
	build_download_url: Callable[[dict], str],
	timezone_label: str = "UTC",
) -> None:
	"""
	Muestra la lista de resultados con enlaces de descarga.
	
	NOTA: Los enlaces usan el atributo 'download' HTML5, pero este solo funciona
	para same-origin. Para cross-origin (Streamlit en 8501, API en 8000), el 
	navegador ignora el atributo 'download' y abre el archivo en una nueva pestaña.
	
	Sin embargo, como el servidor envía el header 'Content-Disposition: attachment',
	el navegador debería descargar el archivo automáticamente en lugar de mostrarlo.
	"""

	if not records:
		st.info("No se encontraron archivos para los filtros seleccionados.")
		return

	table = st.container()
	# Mostrar solo Nombre | Modificado | Descargar (sin mostrar la ruta)
	headers = table.columns([4, 2, 1])
	headers[0].markdown("**Nombre**")
	headers[1].markdown(f"**Modificado ({timezone_label})**")
	headers[2].markdown("**Descargar**")

	for record in records:
		cols = table.columns([4, 2, 1])
		cols[0].markdown(f"**{record['name']}**\n\n`{record.get('size', '?')} bytes`")
		cols[1].markdown(str(record.get("last_modified", "-")))

		# Construir URL de descarga
		download_url = build_download_url(record)
		file_name = record.get("name", "download")

		# Usar enlace HTML simple que abre en nueva pestaña
		cols[2].markdown(
			f'<a href="{download_url}" target="_blank" rel="noopener noreferrer" '
			f'style="text-decoration: none; padding: 4px 8px; background-color: #f0f2f6; '
			f'border-radius: 4px; display: inline-block;">⬇️ Descargar</a>',
			unsafe_allow_html=True
		)


def pagination_controls(
	*,
	page: int,
	has_previous: bool,
	has_next: bool,
) -> Optional[str]:
	"""Renderiza los controles de paginación y devuelve la acción elegida."""

	col_prev, col_info, col_next = st.columns([1, 2, 1])
	action: Optional[str] = None

	if has_previous:
		if col_prev.button("◀ Anterior", use_container_width=True):
			action = "prev"
	else:
		col_prev.write("")

	col_info.markdown(f"**Página {page + 1}**")

	if has_next:
		if col_next.button("Siguiente ▶", use_container_width=True):
			action = "next"
	else:
		col_next.write("")

	return action


def show_error(message: str) -> None:
	st.error(message)


def show_warning(message: str) -> None:
	st.warning(message)


__all__ = [
	"setup_page",
	"search_form",
	"upload_form",
	"render_results",
	"pagination_controls",
	"show_error",
	"show_warning",
]
