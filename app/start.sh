#!/usr/bin/env bash
set -euo pipefail

LOG_DIR=${LOG_DIR:-/app/logs}
FILES_DIR=${FILES_ROOT:-/app/files}
STREAMLIT_PORT=${STREAMLIT_SERVER_PORT:-8501}
API_HOST=${API_HOST:-0.0.0.0}
API_PORT=${API_PORT:-8000}
API_BASE_URL_VALUE=${API_BASE_URL:-http://127.0.0.1:8000}

mkdir -p "${LOG_DIR}" "${FILES_DIR}"
chmod 777 "${LOG_DIR}" "${FILES_DIR}" || true

export PYTHONPATH="/app:${PYTHONPATH:-}"
export API_BASE_URL="${API_BASE_URL_VALUE}"

python -m main &
API_SERVER_PID=$!
trap "kill ${API_SERVER_PID}" EXIT

sleep 2

streamlit run client/app.py \
  --server.address 0.0.0.0 \
  --server.port "${STREAMLIT_PORT}" \
  --server.headless true \
  --browser.gatherUsageStats false

wait ${API_SERVER_PID}
