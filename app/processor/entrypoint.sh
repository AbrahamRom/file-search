#!/bin/bash
set -e

echo "=== Processor Node Entrypoint ==="

# Ensure log directory exists
mkdir -p /app/logs

echo "Starting Processor Node..."
echo "  PROCESSOR_ID: ${PROCESSOR_ID:-processor_1}"
echo "  PROCESSOR_PORT: ${PROCESSOR_PORT:-8000}"
echo "  STORAGE_NODES: ${STORAGE_NODES:-http://storage_1:8000}"

# Run the application
exec python -m processor.main
