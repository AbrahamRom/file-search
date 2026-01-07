"""
Configuración centralizada y segura de CORS para todos los servicios.

Esta configuración se aplica de manera consistente en todos los servicios
(Processor, Storage) según el ambiente (desarrollo/producción).

Los orígenes permitidos se obtienen dinámicamente de variables de entorno,
permitiendo que funcione en arquitecturas distribuidas multi-host.
"""

import os
import logging
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


def _get_dynamic_origins() -> list:
    """
    Obtener orígenes dinámicos desde variables de entorno.
    
    Variables soportadas:
        - BROWSER_API_URL: URL completa del cliente (ej: http://192.168.163.212:8000)
        - PROCESSOR_EXTERNAL_IP: IP externa del procesador
        - CORS_ALLOWED_ORIGINS: Lista adicional separada por comas
    """
    origins = set()
    
    # ═══════════════════════════════════════════════════════════════════
    # 1. BROWSER_API_URL - URL que usa el navegador para acceder al API
    # ═══════════════════════════════════════════════════════════════════
    browser_url = os.getenv("BROWSER_API_URL", "")
    if browser_url:
        # Extraer el origen (scheme + host + port)
        parsed = urlparse(browser_url)
        if parsed.scheme and parsed.netloc:
            origin = f"{parsed.scheme}://{parsed.netloc}"
            origins.add(origin)
            # También agregar variante con puerto del cliente (8501)
            host = parsed.hostname
            if host:
                origins.add(f"http://{host}:8501")
                origins.add(f"http://{host}:8000")
                origins.add(f"http://{host}:8001")
    
    # ═══════════════════════════════════════════════════════════════════
    # 2. PROCESSOR_EXTERNAL_IP - IP externa del procesador actual
    # ═══════════════════════════════════════════════════════════════════
    processor_ip = os.getenv("PROCESSOR_EXTERNAL_IP", "")
    if processor_ip:
        origins.add(f"http://{processor_ip}:8000")
        origins.add(f"http://{processor_ip}:8001")
        origins.add(f"http://{processor_ip}:8501")
    
    # ═══════════════════════════════════════════════════════════════════
    # 3. CORS_ALLOWED_ORIGINS - Orígenes adicionales (separados por coma)
    # ═══════════════════════════════════════════════════════════════════
    extra_origins = os.getenv("CORS_ALLOWED_ORIGINS", "")
    if extra_origins:
        for origin in extra_origins.split(","):
            origin = origin.strip()
            if origin:
                origins.add(origin)
    
    return list(origins)


def get_cors_config() -> dict:
    """
    Obtener configuración CORS segura basada en ambiente.
    
    Returns:
        dict: Configuración para agregar como middleware
        
    Environment Variables:
        ENVIRONMENT: "production", "staging", o "development" (default)
        BROWSER_API_URL: URL del API desde el navegador
        PROCESSOR_EXTERNAL_IP: IP externa del procesador
        CORS_ALLOWED_ORIGINS: Orígenes adicionales (separados por coma)
    """
    
    # ═══════════════════════════════════════════════════════════════════
    # ORÍGENES BASE (siempre permitidos en desarrollo)
    # ═══════════════════════════════════════════════════════════════════
    base_origins = [
        # Localhost (desarrollo local)
        "http://localhost:8501",
        "http://localhost:8000",
        "http://localhost:8001",
        "http://127.0.0.1:8501",
        "http://127.0.0.1:8000",
        # Docker interno
        "http://client:8501",
        "http://processor_1:8000",
        "http://processor_2:8000",
        "http://storage_1:8000",
        "http://storage_2:8000",
        "http://storage_3:8000",
    ]
    
    # ═══════════════════════════════════════════════════════════════════
    # AGREGAR ORÍGENES DINÁMICOS (IPs de variables de entorno)
    # ═══════════════════════════════════════════════════════════════════
    dynamic_origins = _get_dynamic_origins()
    
    # Combinar y eliminar duplicados
    all_origins = list(set(base_origins + dynamic_origins))
    
    # ═══════════════════════════════════════════════════════════════════
    # CONFIGURACIÓN SEGURA
    # ═══════════════════════════════════════════════════════════════════
    
    cors_config = {
        "allow_origins": all_origins,              # ✅ Lista explícita (NO ["*"])
        "allow_credentials": True,                 # Permitir cookies si es necesario
        "allow_methods": ["GET", "POST", "DELETE"],  # ✅ Solo métodos necesarios
        "allow_headers": [
            "Content-Type",
            "Authorization",
        ],
        "expose_headers": [
            "Content-Disposition",
            "Content-Length",
            "X-Total-Count",
        ],
        "max_age": 600,
    }
    
    logger.info(f"CORS Config: {len(all_origins)} allowed origins")
    if dynamic_origins:
        logger.info(f"  Dynamic origins from env: {dynamic_origins}")
    for origin in sorted(all_origins):
        logger.debug(f"  - Allowed: {origin}")
    
    return cors_config


def print_cors_config():
    """Función para debug - muestra la config actual."""
    config = get_cors_config()
    print("\n" + "="*70)
    print("CORS CONFIGURATION")
    print("="*70)
    print(f"Environment: {os.getenv('ENVIRONMENT', 'development')}")
    print(f"BROWSER_API_URL: {os.getenv('BROWSER_API_URL', '(not set)')}")
    print(f"PROCESSOR_EXTERNAL_IP: {os.getenv('PROCESSOR_EXTERNAL_IP', '(not set)')}")
    print(f"\nAllowed Origins ({len(config['allow_origins'])}):")
    for origin in sorted(config['allow_origins']):
        print(f"  ✓ {origin}")
    print(f"\nAllowed Methods: {config['allow_methods']}")
    print(f"Allowed Headers: {config['allow_headers']}")
    print("="*70 + "\n")
