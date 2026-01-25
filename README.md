# yt-dlp-api

High-performance REST API wrapper for [yt-dlp](https://github.com/yt-dlp/yt-dlp), built with FastAPI and Redis. Designed for scalability, speed, and ease of deployment.

## Features

*   🚀 **High Performance**: Built on FastAPI with asynchronous architecture.
*   ⚡ **Fast Downloads**: Integrated **aria2c** support for multi-connection downloads.
*   xxxx **Real-time Progress**: WebSocket support for granular progress tracking.
*   🛡️ **Secure**: Optional API Key authentication and SSRF protection.
*   📈 **Observability**: Prometheus metrics endpoint (`/metrics`) ready for scraping.
*   🔄 **Auto-Update**: Automatically fetches the latest `yt-dlp` binary on container startup to keep up with YouTube changes.
*   ⚖️ **Scalable**: Redis-backed task management and rate limiting.

## Prerequisites

*   Docker
*   Docker Compose

## Quick Start

1.  **Clone the repository**
    ```bash
    git clone https://github.com/yunfie-twitter/yt-dlp-api.git
    cd yt-dlp-api
    ```

2.  **Start with Docker Compose**
    ```bash
    docker-compose up -d
    ```

    The API will be available at `http://localhost:8001`.

## Configuration

Configuration is managed via `app/config/config.json` or environment variables.

### Environment Variables

| Variable | Description | Default |
| :--- | :--- | :--- |
| `PORT` | API listening port (Host) | `8001` |
| `REDIS_URL` | Redis connection URL | `redis://redis:6379` |
| `CONFIG_PATH` | Path to config file inside container | `/app/config/config.json` |

### Configuration File (`config.json`)

To customize behavior, mount a JSON file to `/app/config/config.json`.

**Example:**
```json
{
  "auth": {
    "api_key_enabled": true,
    "issue_allowed_origins": ["https://myapp.com"]
  },
  "download": {
    "max_concurrent": 10,
    "timeout_seconds": 3600,
    "use_aria2c": true,
    "aria2c_max_connections": 16
  },
  "rate_limit": {
    "enabled": true,
    "max_requests": 60,
    "window_seconds": 60
  }
}
```

## API Documentation

### Authentication

When `api_key_enabled` is `true`, all v1 endpoints require authentication.
Pass the API key in the header:
```http
X-API-Key: your_generated_api_key
```

### Endpoints

#### Public
*   `GET /health` - Check service health and Redis connection.
*   `GET /metrics` - Prometheus metrics.
*   `POST /auth/token` - Issue a new API Key (Restricted by Origin).

#### API v1 (Protected)
*   `POST /api/v1/info` - Get metadata for a video URL.
*   `GET /api/v1/search` - Search for videos (`?q=query&limit=5`).
*   `POST /api/v1/download/start` - Start a background download task.
*   `GET /api/v1/task/{task_id}` - Check download status.
*   `GET /api/v1/download/file/{task_id}` - Retrieve the downloaded file.

#### WebSocket
*   `ws://host/api/v1/download/progress/ws/{task_id}` - Subscribe to real-time progress updates.

## Development

### Running Locally

1.  **Install dependencies**
    ```bash
    pip install -r requirements.txt
    ```

2.  **Start Redis**
    ```bash
    docker run -d -p 6379:6379 redis:alpine
    ```

3.  **Run Tests**
    ```bash
    pytest
    ```

4.  **Linting**
    ```bash
    ruff check .
    ```

## Architecture

*   **API**: FastAPI (Python 3.11)
*   **Task Queue / Cache**: Redis
*   **Downloader**: yt-dlp + aria2c
*   **Runtime**: Docker Container

## License

Apache License 2.0
