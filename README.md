# Vast.ai MCP Server

An MCP (Model Context Protocol) server that exposes Vast.ai cloud operations
as tools: list GPUs, search offers, create volumes, create instances, and
view billing.

## Setup

Installed as a package via pip (recommended):

```bash
pip install vastai-mcp          # stdio only
pip install "vastai-mcp[http]"  # adds the HTTP transport's deps (uvicorn, starlette)
export VAST_API_KEY=your_api_key_here
```

Or from source, for local development:

```bash
pip install -e ".[http]"
```

Get an API key at <https://cloud.vast.ai> (Settings -> API Keys).

## Run (stdio, for MCP clients)

Installing the package puts a `vastai-mcp` command on your PATH:

```bash
vastai-mcp
```

## Claude Desktop config example

```json
{
  "mcpServers": {
    "vastai": {
      "command": "vastai-mcp",
      "env": { "VAST_API_KEY": "your_api_key" }
    }
  }
}
```

(See `mcp.json.example` in this repo for a copy-pasteable template — copy it to
`mcp.json` and fill in your key; `mcp.json` itself is gitignored since it holds
a real secret.)

## Run (HTTP, for remote/multi-user deployment)

```bash
vastai-mcp-http
```

The HTTP server holds no Vast.ai key of its own. Every request to `POST /mcp`
must include the caller's own key in an `X-Vast-Api-Key` header — that key is
used only for that request, so rentals and billing land on the caller's Vast.ai
account, not the server operator's. Requests without the header get a 401.
`GET /health` needs no auth.

Deploy behind HTTPS (e.g. Caddy/nginx/Cloudflare in front) — remote MCP
clients such as ChatGPT connectors require TLS.

## Deploy (Docker)

```bash
docker build -t vastai-mcp-http .
docker run --rm -p 8000:8000 vastai-mcp-http
curl http://localhost:8000/health
```

This does not need publishing to PyPI, or even the pip-packaging machinery at
all — the image just installs the runtime deps from `requirements.txt` and
runs `python -m vastai_mcp.server_http` directly (no console-script entry
point involved inside the container). Push the built image to your cloud
provider's registry (or build straight from this repo if the provider
supports that, e.g. Fly.io, Railway, Render), and put a TLS-terminating proxy
or the platform's built-in HTTPS in front of port 8000 before pointing a
ChatGPT connector at it.

## Tools

| Tool | Description |
| --- | --- |
| `list_gpus` | Current GPU supply/demand/pricing snapshot. |
| `search_offers` | Search rentable machine offers (filter by GPU, price, disk, country). |
| `create_volume` | Rent a new persistent volume (searches a matching volume offer and rents it). |
| `list_volumes` | List your rented volumes. |
| `create_instance` | Rent a machine by offer id (requires `max_hourly_price` as a spend cap), optionally creating/attaching a volume. |
| `billing_summary` | Per-instance hourly cost breakdown (GPU, disk, storage, total) plus recent charges. |

### Typical workflow

1. `list_gpus` to see what's available.
2. `search_offers(gpu_name="RTX 4090", max_price=1.0, limit=10)` to find a machine.
3. `billing_summary` to check current spend.
4. `create_instance(offer_id=123, max_hourly_price=1.0, volume={"size_gb": 100, "mount_path": "/data"})`
   to launch (refuses if the offer's live price exceeds `max_hourly_price`).
