"""Interfaz Streamlit para el buscador de archivos."""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Dict, List
from urllib.parse import urljoin

import importlib
import requests
from requests import RequestException

try:
	streamlit = importlib.import_module("streamlit")
except ModuleNotFoundError as exc:  # pragma: no cover - ayuda en entornos incompletos
	raise SystemExit(
		"Streamlit no está instalado. Asegúrate de ejecutar `pip install streamlit requests`."
	) from exc

st = streamlit

from ui_components import (
	pagination_controls,
	render_results,
	search_form,
	setup_page,
	show_error,
	show_warning,
)



API_BASE_URL = os.getenv("API_BASE_URL", "http://localhost:8000")
DEFAULT_PAGE_SIZE = int(os.getenv("CLIENT_PAGE_SIZE", "10"))
DEFAULT_TYPE = "Todos"


def api_url(path: str) -> str:
	return urljoin(API_BASE_URL.rstrip("/") + "/", path.lstrip("/"))


@st.cache_data(ttl=10)
def fetch_files(query: str, *, limit: int, offset: int) -> List[Dict]:
	params = {"query": query, "limit": limit, "offset": offset}
	response = requests.get(api_url("search"), params=params, timeout=15)
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


def main() -> None:
	ensure_state_defaults()
	setup_page()

	query, type_choice, limit_choice, submitted = search_form(
		default_query=st.session_state.query,
		file_types=st.session_state.available_types,
		default_file_type=st.session_state.file_type,
		default_limit=st.session_state.limit,
	)

	if submitted:
		st.session_state.query = query.strip()
		st.session_state.file_type = type_choice
		st.session_state.limit = limit_choice
		st.session_state.page = 0

	if not st.session_state.query:
		show_warning("Introduce un término de búsqueda para comenzar.")
		return

	page = st.session_state.page
	limit = st.session_state.limit
	offset = page * limit

	try:
		raw_results = fetch_files(
			st.session_state.query,
			limit=limit + 1,  # pido uno extra para detectar si hay página siguiente
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
		build_download_url=lambda record: api_url(
			f"files/{record['file_id']}/download"
		),
		timezone_label="local",
	)

	action = pagination_controls(
		page=page,
		has_previous=page > 0,
		has_next=has_next,
	)

	if action == "prev":
		st.session_state.page -= 1
		st.experimental_rerun()
	elif action == "next":
		st.session_state.page += 1
		st.experimental_rerun()


if __name__ == "__main__":
	main()
