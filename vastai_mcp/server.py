"""Vast.ai MCP server.

Exposes Vast.ai cloud operations as MCP tools:
  - search_offers: find rentable GPU machine offers
  - create_volume: rent a new persistent volume
  - list_volumes: list your rented volumes
  - create_instance: rent a machine (ask/offer) with optional volume
  - billing_summary: instance hourly costs + recent charges

Auth: set VAST_API_KEY (Authorization: Bearer <key>).
"""

from __future__ import annotations

import contextvars
import json
import os
import time
from typing import Any

import httpx
import mcp.server.stdio
import mcp_types as types
from mcp.server.lowlevel import Server

BASE_URL = "https://console.vast.ai"

# Set per-request by server_http.py's auth middleware (from the caller's
# X-Api-Key header) so a multi-tenant HTTP deployment bills each caller's
# own Vast.ai account. stdio mode (server.py run directly) never sets this and
# falls back to the VAST_API_KEY environment variable below.
_request_api_key: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "request_api_key", default=None
)

server = Server(
    "vastai-mcp",
    description="Tools for renting GPUs, volumes and tracking billing on Vast.ai",
)

# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------


def _api_key() -> str:
    key = (_request_api_key.get() or "").strip()
    if key:
        return key
    key = os.environ.get("VAST_API_KEY", "").strip()
    if not key:
        raise RuntimeError(
            "No Vast.ai API key available. Set VAST_API_KEY (stdio mode) or "
            "send an X-Api-Key header (HTTP mode). Create a key at "
            "cloud.vast.ai."
        )
    return key


def _http() -> httpx.Client:
    return httpx.Client(
        base_url=BASE_URL,
        headers={"Authorization": f"Bearer {_api_key()}"},
        timeout=60.0,
        follow_redirects=True,
    )


def _request(method: str, path: str, **kwargs: Any) -> dict[str, Any]:
    with _http() as client:
        resp = client.request(method, path, **kwargs)
        if resp.status_code >= 400:
            try:
                body = resp.json()
            except Exception:
                body = {"raw": resp.text}
            raise RuntimeError(
                f"Vast.ai API error {resp.status_code} on {method} {path}: {body}"
            )
        return resp.json()


# ---------------------------------------------------------------------------
# MCP plumbing
# ---------------------------------------------------------------------------

TOOLS: list[types.Tool] = [
    types.Tool(
        name="search_offers",
        description=(
            "Search rentable GPU machine offers on Vast.ai. Returns offers with "
            "id (ask_id), gpu_name, num_gpus, price per hour (dph_total), disk, "
            "region, bandwidth, verification and reliability. Use the returned "
            "offer id with create_instance."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "gpu_name": {
                    "type": "string",
                    "description": "Exact GPU model, e.g. 'RTX 4090'.",
                },
                "num_gpus": {
                    "type": "integer",
                    "description": "Minimum number of GPUs per machine.",
                },
                "max_price": {
                    "type": "number",
                    "description": "Max total price per hour ($).",
                },
                "min_disk": {
                    "type": "number",
                    "description": "Minimum local disk in GB.",
                },
                "country": {
                    "type": "string",
                    "description": "Country code, e.g. 'US'.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max offers to return (default 20).",
                },
                "sort": {
                    "type": "string",
                    "enum": ["price_asc", "price_desc", "score"],
                    "description": "Sort order (default: score).",
                },
                "allocated_storage": {
                    "type": "number",
                    "description": "Assumed storage in GB used for pricing (default 8).",
                },
            },
        },
    ),
    types.Tool(
        name="create_volume",
        description=(
            "Rent a new persistent volume on Vast.ai. The volume is billed "
            "separately from instances. Returns the new volume name/id."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "size_gb": {
                    "type": "integer",
                    "description": "Volume size in GB (default 15).",
                },
            },
            "required": ["size_gb"],
        },
    ),
    types.Tool(
        name="list_volumes",
        description="List all volumes currently rented by you.",
        input_schema={"type": "object", "properties": {}},
    ),
    types.Tool(
        name="create_instance",
        description=(
            "Create (rent) a new instance on a specific machine offer (ask id "
            "from search_offers), optionally creating and attaching a new "
            "volume. Returns the new contract/instance id. Requires "
            "max_hourly_price as a spend guardrail: the offer's live price is "
            "checked against it and the rental is refused if the price "
            "exceeds the cap or can't be verified."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "offer_id": {
                    "type": "integer",
                    "description": "Offer/ask id returned by search_offers.",
                },
                "max_hourly_price": {
                    "type": "number",
                    "description": (
                        "Required spend cap in $/hr. The offer's current "
                        "dph_total is checked against this before renting; "
                        "if it exceeds the cap (or can't be fetched), the "
                        "request is refused."
                    ),
                },
                "image": {
                    "type": "string",
                    "description": "Docker image (default: vastai/base-image).",
                },
                "label": {
                    "type": "string",
                    "description": "Friendly instance name.",
                },
                "disk_gb": {
                    "type": "number",
                    "description": "Local disk in GB (min 8).",
                },
                "runtype": {
                    "type": "string",
                    "enum": [
                        "ssh",
                        "jupyter",
                        "args",
                        "ssh_proxy",
                        "ssh_direct",
                        "jupyter_proxy",
                        "jupyter_direct",
                    ],
                    "description": "Run type (default: ssh).",
                },
                "target_state": {
                    "type": "string",
                    "enum": ["running", "stopped"],
                    "description": "State to bring the instance to (default: running).",
                },
                "env": {
                    "type": "string",
                    "description": "Docker flags, e.g. '-e HF_TOKEN=hf_xxx -p 8000:8000'.",
                },
                "volume": {
                    "type": "object",
                    "description": "Volume to attach, created on the fly if volume_id is omitted.",
                    "properties": {
                        "volume_id": {
                            "type": "integer",
                            "description": "Existing volume id (from list_volumes).",
                        },
                        "size_gb": {
                            "type": "integer",
                            "description": "New volume size in GB (when creating).",
                        },
                        "mount_path": {
                            "type": "string",
                            "description": "Mount path inside the container (default /data).",
                        },
                    },
                },
            },
            "required": ["offer_id", "max_hourly_price"],
        },
    ),
    types.Tool(
        name="billing_summary",
        description=(
            "Overall billing: per-instance hourly cost breakdown (GPU, disk, "
            "storage, total $/hr) plus the most recent charges. "
            "days limits the charge lookback window."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "days": {
                    "type": "integer",
                    "description": "Look back this many days for charges (default 30).",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max charges to return (default 50).",
                },
            },
        },
    ),
]

HANDLERS: dict[str, Any] = {}


def tool(fn: Any) -> Any:
    HANDLERS[fn.__name__] = fn
    return fn


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@tool
def search_offers(
    gpu_name: str | None = None,
    num_gpus: int | None = None,
    max_price: float | None = None,
    min_disk: float | None = None,
    country: str | None = None,
    limit: int = 20,
    sort: str = "score",
    allocated_storage: float = 8,
) -> dict[str, Any]:
    """Search rentable machine offers with filters."""
    q: dict[str, Any] = {
        "limit": limit,
        "type": "on-demand",
        "verified": {"eq": True},
        "rentable": {"eq": True},
        "rented": {"eq": False},
        "external": {"eq": False},
        "allocated_storage": allocated_storage,
    }
    if gpu_name:
        q["gpu_name"] = {"eq": gpu_name}
    if num_gpus is not None:
        q["num_gpus"] = {"gte": num_gpus}
    if max_price is not None:
        q["dph_total"] = {"lte": max_price}
    if min_disk is not None:
        q["disk_space"] = {"gte": min_disk}
    if country:
        q["geolocation"] = {"in": [country]}
    order = {
        "price_asc": [["dph_total", "asc"]],
        "price_desc": [["dph_total", "desc"]],
        "score": [["score", "desc"]],
    }[sort]
    q["order"] = order
    return _request("POST", "/api/v0/bundles/", json=q)


@tool
def create_volume(size_gb: int) -> dict[str, Any]:
    """Rent a new persistent volume.

    The public REST surface exposes PUT /api/v0/volumes (resize) and
    create-on-attach via instance creation. We therefore create a 1-slot
    volume offer through the search + ask flow if a standalone create is not
    available; this helper uses the documented rent-volume path with a fresh
    volume id request.
    """
    # Vast.ai creates volumes on the fly when attaching to an instance
    # (volume_info.create_new). A standalone "create volume" is realized by
    # renting a volume offer: search for a matching volume offer and rent it.
    offers = _request(
        "POST",
        "/api/v0/search/volumes/",
        json={"limit": 10, "size_gb": {"gte": size_gb}},
    )
    vol_offers = offers.get("volumes") or offers.get("offers") or []
    if not vol_offers:
        return {
            "success": False,
            "error": (
                f"No volume offer found with size >= {size_gb} GB. "
                "Try a larger size or attach the volume at instance creation "
                "(create_instance with volume={size_gb, mount_path})."
            ),
        }
    offer = sorted(vol_offers, key=lambda v: v.get("size_gb", 0))[0]
    rent = _request(
        "PUT",
        "/api/v0/volumes",
        json={"id": offer.get("id"), "size": size_gb},
    )
    return rent


@tool
def list_volumes() -> dict[str, Any]:
    """List all rented volumes."""
    return _request("GET", "/api/v0/volumes/")


def _offer_price(offer_id: int) -> float | None:
    """Fetch the live dph_total for a single offer, or None if not found."""
    resp = _request(
        "POST",
        "/api/v0/bundles/",
        json={"limit": 1, "id": {"eq": offer_id}},
    )
    offers = resp.get("offers") or []
    if not offers:
        return None
    return float(offers[0].get("dph_total") or 0.0)


@tool
def create_instance(
    offer_id: int,
    max_hourly_price: float,
    image: str = "vastai/base-image:@vastai-automatic-tag",
    label: str | None = None,
    disk_gb: float | None = None,
    runtype: str = "ssh",
    target_state: str = "running",
    env: str | None = None,
    volume: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create an instance on the given offer, optionally with a volume.

    Refuses to rent if the offer's live price exceeds max_hourly_price or
    can't be verified, so a caller (human or LLM) always states a spend cap
    up front instead of trusting a possibly-stale or hallucinated offer_id.
    """
    price = _offer_price(offer_id)
    if price is None:
        return {
            "success": False,
            "error": (
                f"Could not verify the current price of offer {offer_id} "
                "(it may no longer be available). Refusing to rent without "
                "a verified price. Re-run search_offers to get a fresh "
                "offer_id."
            ),
        }
    if price > max_hourly_price:
        return {
            "success": False,
            "error": (
                f"Offer {offer_id} costs ${price:.4f}/hr, which exceeds the "
                f"max_hourly_price cap of ${max_hourly_price:.4f}/hr. "
                "Raise the cap if this price is acceptable, or search for a "
                "cheaper offer."
            ),
        }
    body: dict[str, Any] = {
        "image": image,
        "runtype": runtype,
        "target_state": target_state,
    }
    if label:
        body["label"] = label
    if disk_gb is not None:
        body["disk"] = disk_gb
    if env:
        body["env"] = env
    if volume:
        vol_info: dict[str, Any] = {"mount_path": volume.get("mount_path", "/data")}
        if volume.get("volume_id"):
            vol_info["create_new"] = False
            vol_info["volume_id"] = volume["volume_id"]
        else:
            vol_info["create_new"] = True
            vol_info["size"] = volume.get("size_gb", 15)
        body["volume_info"] = vol_info
    return _request("PUT", f"/api/v0/asks/{offer_id}", json=body)


@tool
def billing_summary(days: int = 30, limit: int = 50) -> dict[str, Any]:
    """Per-instance hourly costs + recent charges."""
    instances = _request("GET", "/api/v0/instances/")
    inst_list = instances.get("instances") or []
    hourly = []
    total_hourly = 0.0
    for inst in inst_list:
        if not isinstance(inst, dict):
            continue
        s = inst.get("search") or {}
        i = inst.get("instance") or {}
        gpu_hour = float(i.get("gpuCostPerHour") or s.get("gpuCostPerHour") or 0.0)
        disk_hour = float(i.get("diskHour") or s.get("diskHour") or 0.0)
        storage_hour = float(inst.get("storage_total_cost") or 0.0)
        total_hour = float(
            i.get("discountedTotalPerHour")
            or i.get("totalHour")
            or s.get("discountedTotalPerHour")
            or s.get("totalHour")
            or inst.get("dph_total")
            or 0.0
        )
        total_hourly += total_hour
        hourly.append(
            {
                "id": inst.get("id"),
                "label": inst.get("label"),
                "gpu_name": inst.get("gpu_name"),
                "num_gpus": inst.get("num_gpus"),
                "status": inst.get("actual_status"),
                "gpu_cost_per_hour": round(gpu_hour, 6),
                "disk_cost_per_hour": round(disk_hour, 6),
                "storage_cost_per_hour": round(storage_hour, 6),
                "total_cost_per_hour": round(total_hour, 6),
            }
        )

    now = int(time.time())
    filters = json.dumps({"when": {"gte": now - days * 86400, "lte": now}})
    try:
        charges = _request(
            "GET",
            "/api/v1/invoices",
            params={
                "select_filters": filters,
                "limit": min(limit, 200),
                "latest_first": "true",
            },
        )
    except RuntimeError:
        charges = {"success": False, "error": "Could not fetch charges."}

    recent_charges = (charges.get("results") or [])[:limit]
    total_charged = sum(
        abs(float(c.get("amount", 0)))
        for c in recent_charges
        if c.get("type") == "debit"
    )

    return {
        "instances": hourly,
        "total_hourly_cost": round(total_hourly, 6),
        "hours_per_month_estimate": round(total_hourly * 24 * 30, 2),
        "recent_charges": recent_charges,
        "total_charged_last_days": round(total_charged, 2),
    }


# ---------------------------------------------------------------------------
# MCP dispatch
# ---------------------------------------------------------------------------


async def on_list_tools(ctx, params):
    return types.ListToolsResult(tools=TOOLS)


async def on_call_tool(ctx, params):
    name = params.name
    args = dict(params.arguments or {})
    handler = HANDLERS.get(name)
    if handler is None:
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=f"Unknown tool: {name}")],
            is_error=True,
        )
    try:
        result = handler(**args)
    except TypeError as exc:
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=f"Bad arguments: {exc}")],
            is_error=True,
        )
    except Exception as exc:
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=f"Error: {exc}")],
            is_error=True,
        )
    return types.CallToolResult(
        content=[
            types.TextContent(
                type="text", text=json.dumps(result, indent=2, default=str)
            )
        ]
    )


async def main() -> None:
    server = Server(
        "vastai-mcp",
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
    )
    async with mcp.server.stdio.stdio_server() as (read, write):
        await server.run(
            read, write, server.create_initialization_options()
        )


def cli() -> None:
    """Synchronous entry point for the `vastai-mcp` console script."""
    import argparse
    import asyncio

    from . import __version__

    parser = argparse.ArgumentParser(prog="vastai-mcp")
    parser.add_argument("--version", action="version", version=__version__)
    parser.parse_args()

    asyncio.run(main())


if __name__ == "__main__":
    cli()
