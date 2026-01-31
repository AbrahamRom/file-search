# HTTPS para el CLIENTE (Linux) - Proxy Python Personalizado

Esta guía añade un proxy TLS para servir el cliente y el API por HTTPS usando un script de Python personalizado (sin dependencias externas) y certificados locales.

## Resumen
- Mantiene los contenedores base (dns, storage, processor, client).
- Expone HTTPS en 443 con un Proxy Python dentro de la red `file_search_net`.
- Redirige automáticamente HTTP (80) a HTTPS (443).
- Usa nombres locales con SNI: `client.file-search.local` y `api.file-search.local`.

## 1) Preparar hosts y red
- Apunta los hostnames al servidor:

```bash
echo "192.168.202.12 client.file-search.local api.file-search.local" | sudo tee -a /etc/hosts
```

- Crea la red overlay:

```bash
docker network create --driver overlay --attachable file_search_net
```

## 2) Lanzar servicios base
Asegúrate de que el cliente use HTTPS en su URL de API:

```bash
docker run -d \
  --name client_2 \
  --hostname client_2 \
  --network file_search_net \
  -p 8502:8501 \
  -e BROWSER_API_URL=https://api.file-search.local \
  file-search-client:latest
```

## 3) Proxy Python como terminador TLS
Como no podemos descargar Nginx, usamos un proxy programado en Python que solo usa la librería estándar.

### A) Crear el código del Proxy (`proxy.py`)
Crea el archivo con el siguiente contenido:

```python
import http.server
import http.client
import ssl
import socketserver
import threading

import os

def get_config():
    env_config = os.environ.get("PROXY_CONFIG", "")
    if not env_config:
        return {
            "client.file-search.local": "client_1:8501",
            "api.file-search.local": "processor_1:8000",
            "192.168.202.12": "client_1:8501"
        }
    config_map = {}
    for item in env_config.split(","):
        if "=" in item:
            host, target = item.split("=", 1)
            config_map[host.strip()] = target.strip()
    return config_map

CONFIG = get_config()
CERTS = {
    "cert": "/etc/nginx/certs/fullchain.pem",
    "key": "/etc/nginx/certs/privkey.pem"
}

class ProxyHandler(http.server.BaseHTTPRequestHandler):
    def do_request(self):
        host = self.headers.get('Host')
        if host not in CONFIG:
            self.send_error(404, f"Host {host} no configurado")
            return

        target_host, target_port = CONFIG[host].split(':')
        content_length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(content_length) if content_length > 0 else None

        headers = {k: v for k, v in self.headers.items() if k.lower() not in ['host', 'connection']}
        headers['Host'] = host
        headers['X-Real-IP'] = self.client_address[0]
        headers['X-Forwarded-For'] = self.client_address[0]
        headers['X-Forwarded-Proto'] = 'https'

        try:
            conn = http.client.HTTPConnection(target_host, int(target_port))
            conn.request(self.command, self.path, body, headers)
            response = conn.getresponse()

            self.send_response(response.status)
            for k, v in response.getheaders():
                if k.lower() not in ['transfer-encoding', 'connection']:
                    self.send_header(k, v)
            self.end_headers()
            self.wfile.write(response.read())
            conn.close()
        except Exception as e:
            self.send_error(502, f"Bad Gateway: {str(e)}")

    do_GET = do_POST = do_PUT = do_DELETE = do_request

class RedirectHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(301)
        self.send_header('Location', f'https://{self.headers.get("Host")}{self.path}')
        self.end_headers()

def run_https():
    import os
    if not os.path.exists(CERTS['cert']) or not os.path.exists(CERTS['key']):
        print(f"ERROR: Certificados no encontrados en {CERTS['cert']} o {CERTS['key']}")
        print("Usa -v /ruta/en/host/certs:/etc/nginx/certs:ro al ejecutar el contenedor.")
        return

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(CERTS['cert'], CERTS['key'])
    with socketserver.ThreadingTCPServer(('0.0.0.0', 443), ProxyHandler) as httpd:
        httpd.socket = context.wrap_socket(httpd.socket, server_side=True)
        httpd.serve_forever()

if __name__ == "__main__":
    threading.Thread(target=lambda: socketserver.ThreadingTCPServer(('0.0.0.0', 80), RedirectHandler).serve_forever(), daemon=True).start()
    run_https()
```

### B) Crear el Dockerfile (`Dockerfile.proxy`)
```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY proxy.py .
EXPOSE 80 443
CMD ["python", "proxy.py"]
```

### C) Preparar Certificados y Lanzar
1. Crea la carpeta y genera los certificados:

```bash
sudo mkdir -p /srv/file-search/nginx/certs
sudo chown -R $USER:$USER /srv/file-search
mkcert -install
mkcert -key-file /srv/file-search/nginx/certs/privkey.pem \
       -cert-file /srv/file-search/nginx/certs/fullchain.pem \
       client.file-search.local api.file-search.local 192.168.202.12
```

2. Construye y arranca el proxy:

```bash
docker build -t file-search-proxy -f Dockerfile.proxy .

docker run -d \
  --name custom_proxy \
  --network file_search_net \
  -p 80:80 -p 443:443 \
  -e PROXY_CONFIG="client.file-search.local=client_1:8501,api.file-search.local=processor_1:8000,192.168.202.12=client_1:8501" \
  -v /srv/file-search/nginx/certs:/etc/nginx/certs:ro \
  file-search-proxy
```

## 4) Pruebas
- Navegador: `https://client.file-search.local`
- API: `https://api.file-search.local` (debe responder vía proxy).

## 5) Solución de problemas
- Revisa logs del proxy: `docker logs custom_proxy`
- Verifica conectividad: `docker exec custom_proxy ping client_2`
