# HTTPS para el CLIENTE (Linux)

Basado en [deploy.md](file:///d:/distributed-systems/file-search%20(intento-1)/deploy.md), esta guía añade un proxy TLS para servir el cliente y el API por HTTPS usando Nginx con certificados locales.

## Resumen
- Mantiene los contenedores de deploy.md (dns, storage, processor, client).
- Expone HTTPS en 443 con Nginx dentro de la red `file_search_net`.
- Evita “mixed content” sirviendo también el API por HTTPS.
- Usa nombres locales con SNI: `client.file-search.local` y `api.file-search.local`.

## 1) Preparar hosts y red
- Apunta los hostnames al servidor:

```bash
echo "192.168.202.12 client.file-search.local api.file-search.local" | sudo tee -a /etc/hosts
```

- Crea (si no existe) la red overlay:

```bash
docker network create --driver overlay --attachable file_search_net
```

## 2) Lanzar servicios base (igual que deploy.md)
Usa los mismos comandos de [deploy.md](file:///d:/distributed-systems/file-search%20(intento-1)/deploy.md) para dns, storage y processor. Para el cliente, actualiza el `BROWSER_API_URL` a HTTPS:

```bash
docker run -d \
  --name client_2 \
  --hostname client_2 \
  --network file_search_net \
  -p 8502:8501 \
  -e BROWSER_API_URL=https://api.file-search.local \
  file-search-client:latest
```

```bash
docker run -d \
  --name client_1 \
  --hostname client_1 \
  --network file_search_net \
  -p 8501:8501 \
  -e BROWSER_API_URL=https://192.168.202.12:8000 \
  file-search-client:latest
```

## 3) Nginx como proxy TLS (certificados locales)
- Crea las carpetas:

```bash
sudo mkdir -p /srv/file-search/nginx/certs
```

- Genera certificados locales. Opción A (mkcert):

```bash
mkcert -install
mkcert -key-file /srv/file-search/nginx/certs/privkey.pem -cert-file /srv/file-search/nginx/certs/fullchain.pem client.file-search.local api.file-search.local "*.file-search.local" "file-search.local"
```

- Opción B (OpenSSL con SAN):

```bash
sudo tee /srv/file-search/nginx/openssl.cnf >/dev/null <<'EOF'
[req]
default_bits = 2048
prompt = no
default_md = sha256
req_extensions = req_ext
distinguished_name = dn
[dn]
CN = file-search.local
[req_ext]
subjectAltName = @alt_names
[alt_names]
DNS.1 = client.file-search.local
DNS.2 = api.file-search.local
DNS.3 = file-search.local
DNS.4 = *.file-search.local
EOF
openssl req -x509 -nodes -days 825 -newkey rsa:2048 -keyout /srv/file-search/nginx/certs/privkey.pem -out /srv/file-search/nginx/certs/fullchain.pem -config /srv/file-search/nginx/openssl.cnf
```

- Crea el archivo de configuración de Nginx:

```bash
sudo tee /srv/file-search/nginx/nginx.conf >/dev/null <<'EOF'
events {}

http {
    upstream client_upstream {
        server client_2:8501;
    }
    upstream api_upstream {
        server processor:8000;
    }

    server {
        listen 80;
        server_name client.file-search.local api.file-search.local;
        return 301 https://$host$request_uri;
    }

    server {
        listen 443 ssl;
        server_name client.file-search.local;
        ssl_certificate /etc/nginx/certs/fullchain.pem;
        ssl_certificate_key /etc/nginx/certs/privkey.pem;
        ssl_protocols TLSv1.2 TLSv1.3;
        ssl_prefer_server_ciphers on;
        location / {
            proxy_pass http://client_upstream;
            proxy_set_header Host $host;
            proxy_set_header X-Real-IP $remote_addr;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto https;
        }
    }

    server {
        listen 443 ssl;
        server_name api.file-search.local;
        ssl_certificate /etc/nginx/certs/fullchain.pem;
        ssl_certificate_key /etc/nginx/certs/privkey.pem;
        ssl_protocols TLSv1.2 TLSv1.3;
        ssl_prefer_server_ciphers on;
        location / {
            proxy_pass http://api_upstream;
            proxy_set_header Host $host;
            proxy_set_header X-Real-IP $remote_addr;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto https;
        }
    }
}
EOF
```

- Arranca Nginx en la misma red:

```bash
docker run -d \
  --name nginx_proxy \
  --hostname nginx_proxy \
  --network file_search_net \
  -p 80:80 -p 443:443 \
  -v /srv/file-search/nginx/nginx.conf:/etc/nginx/nginx.conf:ro \
  -v /srv/file-search/nginx/certs:/etc/nginx/certs:ro \
  nginx:1.25
```

Notas:
- Si usas mkcert, el sistema confiará en la CA local instalada; con OpenSSL, el navegador puede requerir aceptar el certificado.
- Al estar Nginx en `file_search_net`, puede resolver `client_2` y `processor` por nombre de contenedor.

### Usar el nginx.conf del repositorio (opcional)
- Puedes reutilizar el archivo del repositorio [nginx.conf](file:///d:/distributed-systems/file-search%20(intento-1)/nginx.conf) copiándolo a `/srv/file-search/nginx/nginx.conf`:

```bash
cp /ruta/al/repositorio/nginx.conf /srv/file-search/nginx/nginx.conf
```

## Escenario A: acceso desde otra computadora
- Dos computadoras en la misma red pueden acceder al cliente de la primera mediante su IP y los puertos publicados (80/443), aunque la segunda no ejecute contenedores.
- Pasos en la segunda computadora:
  - Añade en `/etc/hosts`: `192.168.202.12 client.file-search.local api.file-search.local`
  - Asegúrate de que el firewall de la primera permite 80/443.
  - Certificados:
    - Si usaste mkcert en la primera, instala también la CA de mkcert en la segunda para que confíe el certificado.
    - Si usaste OpenSSL, importa el certificado generado en el almacén de confianza de la segunda.
    - Alternativa: usa un certificado real (Let’s Encrypt) con un dominio público.
- Importante:
  - Las redes `bridge` de Docker son locales a cada host y no se comparten entre máquinas.
  - El acceso remoto ocurre vía la IP del host y los puertos publicados, no a través de la red `bridge`.
  - Para redes multi-host nativas entre contenedores, usa `overlay` en Swarm y une ambos nodos al clúster.

## 4) Pruebas
- Abre en el navegador: `https://client.file-search.local`
  - Debes ver el candado y la app del cliente.
- Verifica llamadas al API desde el cliente:
  - Deben ir a `https://api.file-search.local` sin bloqueo por contenido mixto.
- Opcional: prueba el API directo: `https://api.file-search.local` (según exponga UI/health).

## 5) Solución de problemas
- Si el navegador advierte del certificado, acepta/instala la CA local (mkcert) o añade el certificado al almacén de confianza para pruebas.
- Comprueba que los contenedores están en `file_search_net`: `docker inspect <nombre> | jq '.[0].NetworkSettings.Networks'`
- Revisa logs:
  - Nginx: `docker logs nginx_proxy`
  - Cliente: `docker logs client_2`
  - Processor: `docker logs processor_2`
