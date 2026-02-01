import http.server
import http.client
import ssl
import socketserver
import threading
import urllib.parse

import os

# Carga la configuración desde la variable de entorno PROXY_CONFIG
# Formato esperado: "dominio1=contenedor1:puerto1,dominio2=contenedor2:puerto2"
def get_config():
    env_config = os.environ.get("PROXY_CONFIG", "")
    if not env_config:
        # Valores por defecto que coinciden con docker-compose.separated.yml
        return {
            "client.file-search.local": "client:8501",
            "api.file-search.local": "processor:8000",
            "192.168.202.12": "client:8501"
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
    def do_GET(self):
        self.proxy_request()

    def do_POST(self):
        self.proxy_request()

    def do_PUT(self):
        self.proxy_request()

    def do_DELETE(self):
        self.proxy_request()

    def proxy_request(self):
        host = self.headers.get('Host')
        if host not in CONFIG:
            self.send_error(404, f"Host {host} not configured")
            return

        target_host, target_port = CONFIG[host].split(':')
        
        # Read request body
        content_length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(content_length) if content_length > 0 else None

        # Prepare headers
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
        print("HTTPS Proxy started on port 443")
        httpd.serve_forever()

def run_http_redirect():
    with socketserver.ThreadingTCPServer(('0.0.0.0', 80), RedirectHandler) as httpd:
        print("HTTP Redirect started on port 80")
        httpd.serve_forever()

if __name__ == "__main__":
    t = threading.Thread(target=run_http_redirect, daemon=True)
    t.start()
    run_https()
