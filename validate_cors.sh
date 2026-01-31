#!/bin/bash
# Script de validación de CORS seguro
# Verifica que la configuración CORS está correctamente implementada

set -e

echo "╔════════════════════════════════════════════════════════════════╗"
echo "║  VALIDACIÓN DE CORS SEGURO - File Search                       ║"
echo "╚════════════════════════════════════════════════════════════════╝"
echo ""

# Color codes
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

ERRORS=0
WARNINGS=0

# ═══════════════════════════════════════════════════════════════════════
# Función para verificar vulnerabilidades
# ═══════════════════════════════════════════════════════════════════════

check_file_vulnerability() {
    local file=$1
    local pattern=$2
    local description=$3
    
    if grep -q "$pattern" "$file" 2>/dev/null; then
        echo -e "${RED}✗ VULNERABLE:${NC} $file"
        echo "  └─ Problema: $description"
        ((ERRORS++))
        return 1
    else
        return 0
    fi
}

check_file_secure() {
    local file=$1
    local pattern=$2
    local description=$3
    
    if grep -q "$pattern" "$file" 2>/dev/null; then
        echo -e "${GREEN}✓ SEGURO:${NC} $file"
        echo "  └─ $description"
        return 0
    else
        echo -e "${RED}✗ MISSING:${NC} $file"
        echo "  └─ Falta: $description"
        ((ERRORS++))
        return 1
    fi
}

echo "═══════════════════════════════════════════════════════════════════"
echo "1. Verificando vulnerabilidades CORS (allow_origins=['*'])"
echo "═══════════════════════════════════════════════════════════════════"
echo ""

# Buscar la configuración vulnerable
check_file_vulnerability \
    "app/storage/api/endpoints.py" \
    'allow_origins=\["*"\]' \
    "allow_origins=['*'] es CRÍTICO - permite acceso desde cualquier sitio web"

check_file_vulnerability \
    "app/processor/api/endpoints.py" \
    'allow_origins=\["*"\]' \
    "allow_origins=['*'] es CRÍTICO - permite acceso desde cualquier sitio web"

echo ""
echo "═══════════════════════════════════════════════════════════════════"
echo "2. Verificando implementación CORS seguro"
echo "═══════════════════════════════════════════════════════════════════"
echo ""

# Buscar la configuración segura
check_file_secure \
    "app/common/cors_config.py" \
    "def get_cors_config" \
    "Archivo de configuración CORS centralizado creado"

check_file_secure \
    "app/storage/api/endpoints.py" \
    "from ..common.cors_config import get_cors_config" \
    "Storage imports configuración CORS centralizada"

check_file_secure \
    "app/processor/api/endpoints.py" \
    "from ..common.cors_config import get_cors_config" \
    "Processor imports configuración CORS centralizada"

check_file_secure \
    "app/storage/api/endpoints.py" \
    'cors_config = get_cors_config()' \
    "Storage applica configuración CORS segura"

check_file_secure \
    "app/processor/api/endpoints.py" \
    'cors_config = get_cors_config()' \
    "Processor applica configuración CORS segura"

echo ""
echo "═══════════════════════════════════════════════════════════════════"
echo "3. Verificando configuración detallada"
echo "═══════════════════════════════════════════════════════════════════"
echo ""

check_file_secure \
    "app/common/cors_config.py" \
    'allow_origins.*allowed_origins' \
    "Usa lista de orígenes permitidos (NO wildcard)"

check_file_secure \
    "app/common/cors_config.py" \
    'allow_methods.*GET.*POST.*DELETE' \
    "Limita métodos HTTP permitidos"

check_file_secure \
    "app/common/cors_config.py" \
    'Content-Type' \
    "Limita headers permitidos"

echo ""
echo "═══════════════════════════════════════════════════════════════════"
echo "4. Configuración de ambientes"
echo "═══════════════════════════════════════════════════════════════════"
echo ""

check_file_secure \
    "app/common/cors_config.py" \
    'environment == "production"' \
    "Soporte para ambiente PRODUCTION"

check_file_secure \
    "app/common/cors_config.py" \
    'environment == "staging"' \
    "Soporte para ambiente STAGING"

check_file_secure \
    "app/common/cors_config.py" \
    'ENVIRONMENT' \
    "Lee variable ENVIRONMENT"

echo ""
echo "═══════════════════════════════════════════════════════════════════"
echo "RESUMEN"
echo "═══════════════════════════════════════════════════════════════════"
echo ""

if [ $ERRORS -eq 0 ]; then
    echo -e "${GREEN}✓ TODOS LOS CHECKS PASARON${NC}"
    echo ""
    echo "La configuración CORS es segura:"
    echo "  ✓ Sin wildcard allow_origins=['*']"
    echo "  ✓ Configuración centralizada"
    echo "  ✓ Soporte para múltiples ambientes"
    echo "  ✓ Métodos y headers limitados"
    echo ""
    exit 0
else
    echo -e "${RED}✗ ENCONTRADOS $ERRORS PROBLEMAS${NC}"
    echo ""
    echo "Acciones necesarias:"
    echo "  1. Verificar que los archivos están en el lugar correcto"
    echo "  2. Ejecutar: git diff para ver los cambios"
    echo "  3. Revisar: app/common/cors_config.py"
    echo ""
    exit 1
fi
