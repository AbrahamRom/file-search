# File Search

A platform composed of a FastAPI backend and a Streamlit web client to locate, filter, upload, and download files hosted on a shared volume.

## 🎯 Key Features

- **Automated Synchronization:** Background scanning of the configured directory (default `/app/files`) with automatic synchronization to an SQLite database.
- **REST API:** Endpoints for health checks, paginated search, file uploads, metadata queries, and file downloads.
- **Streamlit Web Client:** Intuitive interface featuring search by name, filtering by type, pagination, and direct download links.
- **Unified Logging:** Centralized application and access logs writing to `/app/logs`.
- **Highly Available DNS Architecture:** Includes a primary server, two backup servers, and an intelligent proxy with automatic failover.
- **Containerized Environment:** Separate Docker images for the API (`Dockerfile.server`), the client (`Dockerfile.client`), and DNS components, ready for Docker Swarm or distributed deployments.

## 🌐 High Availability DNS Architecture

The system includes a robust, highly available DNS architecture:

### Components

1. **DNS Proxy** (`dns_proxy` - Port 5350)
   - Single entry point for all DNS requests.
   - Automatic failover between servers.
   - Periodic health checks of all DNS servers.
   - Smart routing to healthy servers.

2. **Primary DNS** (`dns_primary` - Port 5353)
   - Main DNS server handling resolutions.
   - Propagates updates to backup servers.
   - DNS resolution caching.
   - Comprehensive logging of all operations.

3. **DNS Backup 1** (`dns_backup_1` - Port 5354)
   - Backup server that synchronizes with the primary.
   - Periodic synchronization every 30 seconds.
   - Automatically takes control if the primary fails.
   - Maintains a synchronized cache.

4. **DNS Backup 2** (`dns_backup_2` - Port 5355)
   - Independent second-level backup server.
   - Provides additional redundancy.
   - Automatic synchronization with the primary.
   - Third-level failover.

### High Availability Features

- **Automatic Replication:** Backup servers automatically sync with the primary every 30 seconds.
- **Transparent Failover:** The proxy detects failures and redirects traffic without manual intervention.
- **Health Monitoring:** Continuous health checks of all servers every 10 seconds.
- **Distributed Cache:** Each server maintains its own cache for rapid responses.
- **Comprehensive Logging:** All operations are recorded with server identifiers.
- **Automatic Recovery:** Downed servers automatically reintegrate upon recovery.

### Operational Flow

1. Client requests DNS resolution from the proxy (port 5350).
2. Proxy attempts to resolve with the primary server.
3. If primary fails, proxy tries backup_1.
4. If backup_1 fails, proxy tries backup_2.
5. Backup servers sync their cache with the primary every 30 seconds.
6. Health checks update the availability status every 10 seconds.

## 📂 Project Structure

```text
app/
├── main.py              # Entry point (configures logs and exposes the FastAPI app)
├── dns_service/
│   ├── main.py          # DNS server with sync and caching
│   ├── proxy.py         # DNS Proxy with automatic failover
│   ├── logging_config.py# DNS logging configuration
│   ├── Dockerfile       # Image for DNS servers
│   └── Dockerfile.proxy # Image for DNS proxy
├── server/
│   ├── api/endpoints.py # API Endpoints
│   ├── logging_config.py# Centralized logging configuration
│   ├── db/
│   │   ├── db.py        # SQLite initialization
│   │   └── crud.py      # Operations on the files table
│   └── services/
│       ├── scanner.py   # File scanning and synchronization
│       └── file_handler.py
├── client/
│   ├── app.py           # Streamlit interface
│   └── ui_components.py # Reusable UI components
├── files/               # Examples for local testing (mount your own volume in production)
└── logs/                # Target folder for logs (mounting a volume is recommended)
```

## 🚀 Quick Start

### Using Docker Compose (Recommended)

The high availability DNS architecture and the application stack are deployed automatically with Docker Compose.

**1. Set Environment Variables:**
Choose the directory you want to share and optionally define the internal container path.

*For Linux/macOS (Bash):*
```bash
export FILES_SOURCE=/path/to/your/documents
export FILES_ROOT=/app/files
```

*For Windows (PowerShell):*
```powershell
$env:FILES_SOURCE="D:\Library"
$env:FILES_ROOT="/app/files"
```
*(Note: If `FILES_SOURCE` is not defined, it defaults to `./runtime/files`)*

**2. Build and Start Services:**
```bash
docker compose up --build
```

**Available Services:**
- **DNS Proxy:** `http://localhost:5350`
- **Primary DNS:** `http://localhost:5353`
- **DNS Backup 1:** `http://localhost:5354`
- **DNS Backup 2:** `http://localhost:5355`
- **API:** `http://localhost:8000` (Docs at `/docs`)
- **Web Client:** `http://localhost:8501`

### Verifying DNS Status

```bash
# Proxy and overall server status
curl http://localhost:5350/health

# Primary server status
curl http://localhost:5353/health

# Backup servers status
curl http://localhost:5354/health
curl http://localhost:5355/health
```

## 🗃️ File Management & API Endpoints

The project uses a folder as its primary data source. Files placed in the configured `files` directory are automatically scanned and registered in the database.

### Uploading Files via API

You can programmatically upload files using the upload endpoint:

```bash
curl -X POST -F "file=@document.pdf" -F "folder=optional_subfolder" http://localhost:8000/upload
```

**Example Response:**
```json
{
  "status": "success",
  "file_id": "7f8e9d1c2b3a4f5e6d7c8b9a",
  "filename": "document.pdf",
  "size": 12345
}
```

### Main API Endpoints

| Method | Path                         | Description                               |
|--------|------------------------------|-------------------------------------------|
| GET    | `/health`                    | Checks service health status.             |
| GET    | `/search?query&limit&offset` | Paginated file search.                    |
| GET    | `/files/{file_id}`           | Retrieves full file metadata.             |
| GET    | `/files/{file_id}/download`  | Downloads the specified file.             |
| POST   | `/files` / `/upload`         | Uploads or updates a file.                |
| DELETE | `/files/{file_id}`           | Deletes an existing file record.          |

*Interactive documentation is automatically generated by FastAPI and available at `http://localhost:8000/docs`.*

## 🧾 Logging

- Logs are stored in `/app/logs` (`application.log` and `access.log`).
- Modify the destination or logging levels using the `LOG_DIR`, `LOG_LEVEL`, and `ACCESS_LOG_LEVEL` environment variables.
- Mount a volume to `/app/logs` to persist logs in a production environment.

## 🛠️ Local Development (Optional)

```bash
pip install -r app/server/requirements.txt
FILES_ROOT=./app/files bash app/start.sh
```
The `app/files` directory in the repository contains examples for quick local testing. For real-world usage, point `FILES_ROOT` to the directory you wish to index.

## 🧪 Testing

```bash
PYTHONPATH=$(pwd) python3 -m pytest tests
```

To manually validate endpoints using the FastAPI `TestClient` (if you haven't written tests yet):
```bash
PYTHONPATH=$(pwd) python3 -c "from fastapi.testclient import TestClient; from app.server.api.endpoints import app; client = TestClient(app); print(client.get('/health').json())"
```

## 📌 Additional Notes

- When building the server image, data files are not packaged. You must mount the desired folder to `/app/files` at runtime.
- For Docker Swarm or Kubernetes deployments, declare volumes/claims for `/app/files` and `/app/logs`, and expose the `API_BASE_URL` variable in the client so it can successfully reach the API.
- Adjust `DB_PATH` if you want the SQLite database to persist outside the container.
