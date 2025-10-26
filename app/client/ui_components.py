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
		"Explora, filtra y descarga los documentos disponibles en el repositorio compartido."
	)


def search_form(
	*,
	default_query: str,
	file_types: Iterable[str],
	default_file_type: str,
	default_limit: int,
	on_submit=None,
	limit_options: Iterable[int] = (10, 20, 50),
) -> tuple[str, str, int, bool]:
	"""Renderiza el formulario de búsqueda y devuelve los valores introducidos."""

	options = list(file_types)
	limits = list(limit_options)

	with st.form("search_form", clear_on_submit=False):
		st.text_input(
			"Buscar por nombre o ruta",
			value=default_query,
			placeholder="Ej. reporte",
			key="search_query"
		)
		col_type, col_limit = st.columns([2, 1])
		with col_type:
			st.selectbox(
				"Tipo de archivo",
				options=options,
				index=max(0, options.index(default_file_type))
				if default_file_type in options
				else 0,
				key="search_type"
			)
		with col_limit:
			st.selectbox(
				"Resultados por página",
				options=limits,
				index=limits.index(default_limit)
				if default_limit in limits
				else 0,
				key="search_limit"
			)

		submitted = st.form_submit_button("Buscar", on_click=on_submit if on_submit else None)

	# Para mantener compatibilidad con el código existente
	query = st.session_state.get("search_query", default_query)
	type_choice = st.session_state.get("search_type", default_file_type)
	limit_choice = st.session_state.get("search_limit", default_limit)

	return query, type_choice, limit_choice, submitted


def render_results(
	records: List[dict],
	*,
	build_download_url: Callable[[dict], str],
	timezone_label: str = "UTC",
) -> None:
	"""Muestra la lista de resultados con botones de descarga."""

	if not records:
		st.info("No se encontraron archivos para los filtros seleccionados.")
		return

	table = st.container()
	headers = table.columns([3, 2, 2, 1])
	headers[0].markdown("**Nombre**")
	headers[1].markdown("**Ruta**")
	headers[2].markdown(f"**Modificado ({timezone_label})**")
	headers[3].markdown("**Descargar**")

	for record in records:
		cols = table.columns([3, 2, 2, 1])
		cols[0].markdown(f"**{record['name']}**\n\n`{record['size']} bytes`")
		cols[1].code(record["path"], language="text")
		cols[2].markdown(str(record.get("last_modified", "-")))
		download_url = build_download_url(record)
		cols[3].markdown(f"[⬇ Descarga]({download_url})")


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
	"render_results",
	"pagination_controls",
	"show_error",
	"show_warning",
]
