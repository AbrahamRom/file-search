FROM haproxy:2.8

# Copiamos la configuración de HAProxy dentro de la imagen
COPY haproxy.cfg /usr/local/etc/haproxy/haproxy.cfg

# Exponer el puerto del frontend
EXPOSE 8501

# Comando por defecto: iniciar haproxy con la configuración copiada
CMD ["haproxy", "-f", "/usr/local/etc/haproxy/haproxy.cfg"]