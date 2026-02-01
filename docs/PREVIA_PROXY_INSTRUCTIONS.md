# 🛡️ Instrucciones Previas al Despliegue del Proxy Seguro

Este documento detalla los requisitos y pasos críticos para habilitar el Proxy TLS/HTTPS en un entorno distribuido usando Docker Swarm.

## 1. Biblioteca Específica para CA: `mkcert`

Para que el proxy funcione con HTTPS y sea reconocido como seguro por el navegador del cliente (evitando advertencias constantes), es **indispensable** el uso de `mkcert`.

### ¿Por qué `mkcert`?
A diferencia de otros generadores, `mkcert`:
- Crea una **Entidad de Certificación (CA)** de confianza.
- Permite generar certificados que incluyen explícitamente la **IP del cliente/host**, lo cual es crítico para que las descargas funcionen fuera del entorno Docker.

### Instalación de `mkcert` (Linux):
```bash
sudo apt install libnss3-tools
curl -JLO "https://dl.filippo.io/mkcert/latest?for=linux/amd64"
chmod +x mkcert-v*-linux-amd64
sudo cp mkcert-v*-linux-amd64 /usr/local/bin/mkcert
```

---

## 2. Configuración en Red Swarm Overlay

En un entorno de producción o multi-host, el proxy debe operar dentro de una red **Overlay** para alcanzar los servicios internos (`client`, `processor`) sin exponerlos directamente al exterior.

### Requisitos de la Red:
- Debe ser de tipo `overlay`.
- Debe tener habilitado el flag `--attachable` para permitir que los contenedores `docker run` (como el proxy manual) se unan a la red del stack.

---

## 3. Comandos de Instalación y Ejecución

Sigue estos comandos en el nodo **Manager** para preparar y lanzar el proxy:

### A) Preparar la Red (si no existe)
```bash
docker network create --driver overlay --attachable file_search_net
```

### B) Generar Certificados con la IP del Cliente
Reemplaza `192.168.x.x` con la IP real del host donde los usuarios accederán:
```bash
sudo mkdir -p /srv/file-search/nginx/certs
sudo chown -R $USER:$USER /srv/file-search

# Instalar la CA en el sistema
mkcert -install

# Generar certificado para dominios e IP del host
mkcert -key-file /srv/file-search/nginx/certs/privkey.pem \
       -cert-file /srv/file-search/nginx/certs/fullchain.pem \
       client.file-search.local api.file-search.local 192.168.x.x
```

### C) Construir y Ejecutar el Proxy
```bash
docker build -t file-search-proxy -f Dockerfile.proxy .

docker run -d \
  --name proxy \
  --network file_search_net \
  -p 80:80 -p 443:443 \
  -v /srv/file-search/nginx/certs:/etc/nginx/certs:ro \
  file-search-proxy
```

---

## 4. Verificación
1. Agrega el mapeo en tu `/etc/hosts`: `192.168.x.x client.file-search.local api.file-search.local`.

