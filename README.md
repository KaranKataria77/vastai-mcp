# Vast.ai MCP Server

An MCP (Model Context Protocol) server that exposes Vast.ai cloud operations
as tools: list GPUs, search offers, create volumes, create instances, and
view billing.

## Setup

Installed as a package via pip (recommended):

```bash
pip install vastai-mcp
export VAST_API_KEY=your_api_key_here
```

Or from source, for local development:

```bash
pip install -e .
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

## Tools

| Tool | Description |
| --- | --- |
| `search_offers` | Search rentable machine offers (filter by GPU, price, disk, country). |
| `create_volume` | Rent a new standalone persistent volume (finds a GPU offer's free volume slot and rents it). |
| `list_volumes` | List your rented volumes. |
| `delete_volume` | Delete a rented volume by id, stopping its billing. |
| `create_instance` | Rent a machine by offer id (requires `max_hourly_price` as a spend cap), optionally creating/attaching a volume. |
| `destroy_instance` | Terminate a rented instance by id, stopping its billing. |
| `billing_summary` | Per-instance hourly cost breakdown (GPU, disk, storage, total) plus recent charges. |

### Typical workflow

1. `search_offers(gpu_name="RTX 4090", max_price=1.0, limit=10)` to find a machine.
2. `billing_summary` to check current spend.
3. `create_instance(offer_id=123, max_hourly_price=1.0, volume={"size_gb": 100, "mount_path": "/data"})`
   to launch (refuses if the offer's live price exceeds `max_hourly_price`).
4. `destroy_instance(instance_id=...)` / `delete_volume(volume_id=...)` to tear down when done.
