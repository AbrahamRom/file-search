Conocer la IP: `ip config`

docker swarm init --advertise-addr 192.168.202.12
docker network create --driver overlay --attachable file_search_net

# DNS Service
docker build -t file-search-dns:latest -f app/dns_service/Dockerfile .

# Storage
docker build -t file-search-storage:latest -f Dockerfile.storage .

# Processor
docker build -t file-search-processor:latest -f Dockerfile.processor .

# Client
docker build -t file-search-client:latest -f Dockerfile.client .

docker rm --force $(docker ps -aq)

docker run -d \
  --name dns_1 \
  --hostname dns_1 \
  --network file_search_net \
  --network-alias dns \
  -p 5353:5353 \
  -v /srv/file-search/logs:/app/logs \
  -e DNS_SERVER_ID=dns_1 \
  file-search-dns:latest
  
docker run -d \
  --name storage_1 \
  --hostname storage_1 \
  --network file_search_net \
  --network-alias storage \
  -p 9000:8000 \
  -v /srv/file-search/files:/tmp/source_files:ro \
  -v /srv/file-search/logs:/app/logs \
  -e STORAGE_ID=storage_1 \
  file-search-storage:latest
  
docker run -d \
  --name storage_2 \
  --hostname storage_2 \
  --network file_search_net \
  --network-alias storage \
  -p 9001:8000 \
  -v /srv/file-search/files:/tmp/source_files:ro \
  -v /srv/file-search/logs:/app/logs \
  -e STORAGE_ID=storage_2 \
  file-search-storage:latest

docker run -d \
  --name processor_1 \
  --hostname processor_1 \
  --network file_search_net \
  --network-alias processor \
  -p 8000:8000 \
  -v /srv/file-search/logs:/app/logs \
  -e PROCESSOR_ID=processor_1 \
  -e PROCESSOR_EXTERNAL_PORT=8000 \
  -e PROCESSOR_EXTERNAL_IP=192.168.202.12 \
  file-search-processor:latest

docker run -d \
  --name processor_2 \
  --hostname processor_2 \
  --network file_search_net \
  --network-alias processor \
  -p 8001:8000 \
  -v /srv/file-search/logs:/app/logs \
  -e PROCESSOR_ID=processor_2 \
  -e PROCESSOR_EXTERNAL_PORT=8001 \
  -e PROCESSOR_EXTERNAL_IP=192.168.202.12 \
  file-search-processor:latest

docker run -d \
  --name processor_3 \
  --hostname processor_3 \
  --network file_search_net \
  --network-alias processor \
  -p 8002:8000 \
  -v /srv/file-search/logs:/app/logs \
  -e PROCESSOR_ID=processor_3 \
  -e PROCESSOR_EXTERNAL_PORT=8002 \
  -e PROCESSOR_EXTERNAL_IP=192.168.202.12 \
  file-search-processor:latest

docker run -d \
  --name client_1 \
  --hostname client_1 \
  --network file_search_net \
  -p 8501:8501 \
  -e BROWSER_API_URL=http://192.168.202.12:8000 \
  file-search-client:latest
