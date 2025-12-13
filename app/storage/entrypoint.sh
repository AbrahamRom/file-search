#!/bin/bash
set -e

echo "=== Storage Node Entrypoint ==="
echo "Timestamp: $(date -Iseconds)"
echo "Hostname: $(hostname)"
echo "STORAGE_ID: ${STORAGE_ID:-storage_1}"

# Ensure directories exist first
mkdir -p /app/files /app/logs /app/data

# Copy source files if available (for initial seeding)
if [ -d "/tmp/source_files" ] && [ "$(ls -A /tmp/source_files 2>/dev/null)" ]; then
    echo "=== Initial File Seeding ==="
    echo "Source directory: /tmp/source_files"
    
    # Count source files
    SOURCE_COUNT=$(find /tmp/source_files -type f 2>/dev/null | wc -l)
    echo "Source files found: $SOURCE_COUNT"
    
    if [ "$SOURCE_COUNT" -gt 0 ]; then
        echo "Copying source files to /app/files..."
        
        # Copy with verbose output for logging
        cp -rv /tmp/source_files/* /app/files/ 2>&1 | head -50 || true
        
        # If more than 50 files, show summary
        if [ "$SOURCE_COUNT" -gt 50 ]; then
            echo "... (showing first 50 files only)"
        fi
        
        # Verify copy
        DEST_COUNT=$(find /app/files -type f 2>/dev/null | wc -l)
        echo "Destination files after copy: $DEST_COUNT"
        
        if [ "$DEST_COUNT" -eq "$SOURCE_COUNT" ]; then
            echo "SUCCESS: All $SOURCE_COUNT files copied successfully"
        elif [ "$DEST_COUNT" -gt 0 ]; then
            echo "PARTIAL: Copied $DEST_COUNT of $SOURCE_COUNT files"
        else
            echo "WARNING: No files were copied"
        fi
        
        # Show total size
        TOTAL_SIZE=$(du -sh /app/files 2>/dev/null | cut -f1)
        echo "Total size in /app/files: $TOTAL_SIZE"
    fi
else
    echo "No source files to copy (directory /tmp/source_files is empty or does not exist)"
fi

echo "=== Directory Status ==="
echo "Files directory (/app/files):"
ls -la /app/files 2>/dev/null | head -20 || echo "  (empty or not accessible)"
FILE_COUNT=$(find /app/files -type f 2>/dev/null | wc -l)
echo "  Total files: $FILE_COUNT"

echo "Data directory (/app/data):"
ls -la /app/data 2>/dev/null | head -10 || echo "  (empty or not accessible)"

echo "Logs directory (/app/logs):"
ls -la /app/logs 2>/dev/null | head -10 || echo "  (empty or not accessible)"

# Set permissions
chmod -R 777 /app/files /app/logs /app/data 2>/dev/null || true

echo "=== Starting Storage Node ==="
echo "  STORAGE_ID: ${STORAGE_ID:-storage_1}"
echo "  STORAGE_PORT: ${STORAGE_PORT:-8000}"
echo "  FILES_ROOT: ${FILES_ROOT:-/app/files}"
echo "  DB_PATH: ${DB_PATH:-/app/data/storage.db}"
echo "  DNS_ALIAS: ${DNS_ALIAS:-dns}"
echo "  DNS_SERVICE_PORT: ${DNS_SERVICE_PORT:-5353}"
echo "=========================================="

# Run the application
exec python -m storage.main
