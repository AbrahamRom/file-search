#!/bin/bash
set -e

# ============================================================================
# SCRIPT DE INICIO DEL SERVIDOR
# Copia los archivos desde el directorio de origen al directorio interno
# Los logs permanecen como volumen persistente
# ============================================================================

# Directorio interno donde el servidor busca los archivos
INTERNAL_FILES_DIR="${FILES_ROOT:-/app/files}"

# Directorio temporal de montaje para copiar archivos (solo lectura)
SOURCE_FILES_DIR="${FILES_SOURCE_PATH:-/tmp/source_files}"

echo "============================================"
echo "Iniciando servidor de archivos..."
echo "============================================"

# Crear directorio interno si no existe
mkdir -p "$INTERNAL_FILES_DIR"

# Verificar si hay archivos para copiar
if [ -d "$SOURCE_FILES_DIR" ] && [ "$(ls -A $SOURCE_FILES_DIR 2>/dev/null)" ]; then
    echo "Copiando archivos desde $SOURCE_FILES_DIR a $INTERNAL_FILES_DIR..."
    
    # Copiar todos los archivos preservando estructura y permisos
    cp -r "$SOURCE_FILES_DIR"/* "$INTERNAL_FILES_DIR"/ 2>/dev/null || true
    
    # Contar archivos copiados
    FILE_COUNT=$(find "$INTERNAL_FILES_DIR" -type f | wc -l)
    echo "Se copiaron $FILE_COUNT archivo(s) al contenedor."
else
    echo "ADVERTENCIA: No se encontraron archivos en $SOURCE_FILES_DIR"
    echo "El servidor iniciará con el directorio de archivos vacío."
fi

echo "Directorio de archivos: $INTERNAL_FILES_DIR"
echo "Contenido del directorio:"
ls -la "$INTERNAL_FILES_DIR" 2>/dev/null || echo "(vacío)"
echo "============================================"

# Iniciar el servidor Python
echo "Iniciando servidor Python..."
exec python -m main
