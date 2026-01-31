# Propuesta de Implementación de HTTPS en Cliente (Streamlit)

Esta propuesta detalla los pasos para asegurar la comunicación entre el navegador del usuario y la interfaz del cliente (Streamlit) utilizando HTTPS con certificados autofirmados.

## Objetivo
Cifrar el tráfico entre el usuario final y la aplicación cliente para proteger la confidencialidad e integridad de la sesión, cumpliendo con el requisito de seguridad en la capa de cliente.

## Estrategia Seleccionada
Utilizar la configuración nativa de SSL/TLS de Streamlit. Esta opción es la más directa y no requiere añadir contenedores adicionales (como Nginx) ni modificar la infraestructura del backend (servidor).

## Pasos de Implementación

### 1. Generación de Certificados
Se generarán certificados autofirmados utilizando `openssl`. Estos archivos se almacenarán en una carpeta segura dentro del contexto de construcción del cliente (ej. `app/client/certs`).

```bash
openssl req -x509 -newkey rsa:4096 -nodes -out cert.pem -keyout key.pem -days 365 -subj "/CN=localhost"
```

### 2. Modificación de `Dockerfile.client`
Se modificará el Dockerfile del cliente para copiar los certificados al interior del contenedor.

```dockerfile
# ... (existente)
COPY app/client/certs /app/certs
# ...
```

### 3. Configuración de Streamlit
Se actualizará el comando de inicio (`CMD`) en el `Dockerfile.client` (o en las variables de entorno) para activar el modo HTTPS apuntando a los certificados.

**Opción A: Argumentos de línea de comandos**
```bash
streamlit run client/app.py \
    --server.sslCertFile=/app/certs/cert.pem \
    --server.sslKeyFile=/app/certs/key.pem \
    ...
```

**Opción B: Configuración via `.streamlit/config.toml`**
Crear un archivo de configuración que se copie al contenedor.

### 4. Validación
1.  Reconstruir el contenedor del cliente: `docker-compose build client`.
2.  Iniciar el servicio.
3.  Acceder a `https://localhost:8501`.
4.  **Nota**: Al ser certificados autofirmados, el navegador mostrará una advertencia de seguridad que deberá ser aceptada manualmente por el usuario.

## Estado de Implementación
**✅ IMPLEMENTADO**

Se han realizado las siguientes acciones:
1.  **Generación de Certificados**: Se han creado `cert.pem` y `key.pem` en `app/client/certs/` utilizando openssl.
2.  **Actualización de Dockerfile**: Se ha modificado `Dockerfile.client` para:
    *   Copiar la carpeta de certificados al contenedor (`/app/certs`).
    *   Iniciar Streamlit con los flags `--server.sslCertFile` y `--server.sslKeyFile`.

Para probar los cambios, es necesario reconstruir la imagen del cliente:
```bash
docker-compose build client
docker-compose up -d client
```

Luego, acceder a **https://localhost:8501** (aceptar la advertencia de seguridad).

---
## Impacto en el Sistema
*   **Transparencia**: No afecta a la comunicación entre el Cliente y el Backend (Processor/Storage), ya que esa comunicación interna sigue ocurriendo sobre HTTP dentro de la red Docker (o podría cifrarse por separado si se requiriera, pero está fuera del alcance de "capa cliente").
*   **Rendimiento**: Impacto despreciable en el rendimiento para el volumen de tráfico esperado.
*   **Experiencia de Usuario**: El usuario verá el candado de seguridad (con advertencia inicial) en su navegador.

## Prerrequisitos
*   Tener `openssl` instalado en el entorno de desarrollo para generar los certificados iniciales.

---
*Esta propuesta cumple con la restricción de no modificar los componentes del servidor (backend), limitándose estrictamente al contenedor y configuración de la aplicación cliente.*
