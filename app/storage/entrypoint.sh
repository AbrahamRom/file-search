#!/bin/bash
set -e

echo "=== Storage Node Entrypoint ==="

# Copy source files if available (for initial seeding)
if [ -d "/tmp/source_files" ] && [ "$(ls -A /tmp/source_files 2>/dev/null)" ]; then
    echo "Copying source files to /app/files..."
    cp -r /tmp/source_files/* /app/files/ 2>/dev/null || true
    echo "Files copied successfully"
else
    echo "No source files to copy"
fi

# Ensure directories exist
mkdir -p /app/files /app/logs /app/data

# Set permissions
chmod -R 777 /app/files /app/logs /app/data 2>/dev/null || true

echo "Starting Storage Node..."
echo "  STORAGE_ID: ${STORAGE_ID:-storage_1}"
echo "  STORAGE_PORT: ${STORAGE_PORT:-8000}"
echo "  FILES_ROOT: ${FILES_ROOT:-/app/files}"
echo "  DB_PATH: ${DB_PATH:-/app/data/storage.db}"

# Run the application
exec python -m storage.main
