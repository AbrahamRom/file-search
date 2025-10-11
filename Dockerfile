FROM python:3.11-slim

ENV DEBIAN_FRONTEND=noninteractive \
	PYTHONUNBUFFERED=1

WORKDIR /app

COPY app/server/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt && rm /tmp/requirements.txt

COPY app/ /app/

RUN chmod +x /app/start.sh

ENV PYTHONPATH=/app

EXPOSE 8000
EXPOSE 8501

CMD ["/app/start.sh"]