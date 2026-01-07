"""
Configuración centralizada y segura de CORS para todos los servicios.

Esta configuración se aplica de manera consistente en todos los servicios
(Processor, Storage) según el ambiente (desarrollo/producción).
"""

import os
import logging

logger = logging.getLogger(__name__)


def get_cors_config() -> dict:
    """
    Obtener configuración CORS segura basada en ambiente.
    
    Returns:
        dict: Configuración para agregar como middleware
        
    Environment Variables:
        ENVIRONMENT: "production", "staging", o "development" (default)
    """
    environment = os.getenv("ENVIRONMENT", "development").lower()
    
    # ═══════════════════════════════════════════════════════════════════
    # ORÍGENES PERMITIDOS POR AMBIENTE
    # ═══════════════════════════════════════════════════════════════════
    
    if environment == "production":
        # ✅ PRODUCCIÓN: Solo dominios explícitos
        allowed_origins = [
            "https://myapp.example.com",      # Tu dominio principal
            "https://app.example.com",        # Alternativa
        ]
        logger.warning("CORS configured for PRODUCTION with restricted origins")
        
    elif environment == "staging":
        # ✅ STAGING: Menos restrictivo pero aún seguro
        allowed_origins = [
            "https://staging.example.com",
            "https://staging-app.example.com",
        ]
        logger.info("CORS configured for STAGING")
        
    else:
        # ✅ DESARROLLO: Permite localhost pero sin allow_origins=["*"]
        allowed_origins = [
            "http://localhost:8501",          # Streamlit (cliente)
            "http://127.0.0.1:8501",          # Loopback
            "http://localhost:3000",          # React app (si aplica)
            "http://localhost:8000",          # Local API testing
            "http://localhost:8080",          # Alternativa
            "http://client:8501",             # Docker Compose
            "http://processor_1:8000",        # Docker Compose
            "http://storage_1:8000",          # Docker Compose
        ]
        logger.info("CORS configured for DEVELOPMENT")
    
    # ═══════════════════════════════════════════════════════════════════
    # CONFIGURACIÓN SEGURA
    # ═══════════════════════════════════════════════════════════════════
    
    cors_config = {
        "allow_origins": allowed_origins,         # ✅ Lista explícita (NO ["*"])
        "allow_credentials": True,                 # Permitir cookies si es necesario
        "allow_methods": ["GET", "POST", "DELETE"],  # ✅ Solo métodos necesarios (NO ["*"])
        "allow_headers": [
            "Content-Type",
            "Authorization",                   # Para Bearer tokens
        ],
        "expose_headers": [
            "Content-Disposition",            # Para descargas
            "Content-Length",
            "X-Total-Count",                  # Para paginación
        ],
        "max_age": 600,                       # Cache de preflight por 10 minutos
    }
    
    logger.info(f"CORS Config: {len(allowed_origins)} allowed origins")
    for origin in allowed_origins:
        logger.debug(f"  - Allowed: {origin}")
    
    return cors_config


def print_cors_config():
    """Función para debug - muestra la config actual."""
    config = get_cors_config()
    print("\n" + "="*70)
    print("CORS CONFIGURATION")
    print("="*70)
    print(f"Environment: {os.getenv('ENVIRONMENT', 'development')}")
    print(f"\nAllowed Origins ({len(config['allow_origins'])}):")
    for origin in config['allow_origins']:
        print(f"  ✓ {origin}")
    print(f"\nAllowed Methods: {config['allow_methods']}")
    print(f"Allowed Headers: {config['allow_headers']}")
    print("="*70 + "\n")
