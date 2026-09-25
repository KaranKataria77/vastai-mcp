"""Vast.ai MCP server.

Exposes Vast.ai cloud operations as MCP tools:
  - search_offers: find rentable GPU machine offers
  - create_volume: rent a new standalone persistent volume
  - list_volumes: list your rented volumes
  - delete_volume: delete a rented volume
  - create_instance: rent a machine (ask/offer) with optional volume
  - destroy_instance: terminate a rented instance
  - list_deleted_instances: history of previously destroyed instances
  - search_base_images: search available docker image templates
  - get_ssh_connection: SSH command/details for a running instance
  - get_instance_endpoint: public http URL(s) for an instance's exposed ports
  - get_instance_logs: fetch container/daemon logs for an instance
  - create_api_key: create a new (optionally scoped) Vast.ai API key
  - list_api_keys: list existing API keys on the account
  - delete_api_key: revoke an existing API key by id
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
        name="delete_volume",
        description=(
            "Delete a rented volume by its id (from list_volumes), stopping "
            "its billing immediately. Irreversible."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "volume_id": {
                    "type": "integer",
                    "description": "Volume id to delete, as returned by list_volumes or create_volume.",
                },
            },
            "required": ["volume_id"],
        },
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
                "onstart": {
                    "type": "string",
                    "description": (
                        "Shell command(s) to run after the instance's SSH/"
                        "Jupyter entrypoint initializes (runtype ssh/jupyter/"
                        "*_direct/*_proxy). E.g. 'vllm serve $MODEL_ID "
                        "--port 8000 --host 0.0.0.0' to serve a model with "
                        "a pre-built vLLM image, combined with env to set "
                        "MODEL_ID and expose the port."
                    ),
                },
                "args_str": {
                    "type": "string",
                    "description": (
                        "Arguments appended to the image's Docker CMD "
                        "(entrypoint preserved). Only used when runtype is "
                        "'args'; ignored otherwise."
                    ),
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
    types.Tool(
        name="destroy_instance",
        description=(
            "Destroy (terminate) a rented instance by its contract/instance id "
            "(the new_contract id returned by create_instance). This stops "
            "billing for the instance immediately and is irreversible."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "instance_id": {
                    "type": "integer",
                    "description": (
                        "Instance/contract id to destroy, as returned by "
                        "create_instance (new_contract) or billing_summary."
                    ),
                },
            },
            "required": ["instance_id"],
        },
    ),
    types.Tool(
        name="list_deleted_instances",
        description=(
            "List previously destroyed/terminated instances, most recent "
            "first, sourced from the account's audit log (there is no "
            "dedicated Vast.ai endpoint for destroyed-instance records; "
            "once destroyed, the full instance record like gpu_name/image "
            "is gone, so this returns instance_id, when it was created "
            "(rental start, if the matching create event is still within "
            "the audit log), when it was destroyed, and the rental "
            "duration in seconds)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "Max destroyed instances to return (default 50).",
                },
            },
        },
    ),
    types.Tool(
        name="search_base_images",
        description=(
            "Search available docker image templates on Vast.ai to use as "
            "the image for create_instance. Returns id, name, image, tag, "
            "and whether it's SSH-capable/recommended. Pass a query to "
            "filter by name/image substring, or leave empty for the "
            "recommended set."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Case-insensitive substring to match against template name or image (e.g. 'pytorch', 'comfyui').",
                },
                "recommended_only": {
                    "type": "boolean",
                    "description": "Only return Vast.ai-recommended templates (default true when query is empty, false otherwise).",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max templates to return (default 30).",
                },
            },
        },
    ),
    types.Tool(
        name="get_ssh_connection",
        description=(
            "Get the SSH connection details/command for a running instance "
            "by its contract/instance id. Returns the ready-to-use ssh "
            "command (root@ssh_host:ssh_port) plus the raw host/port fields. "
            "Fails with a clear message if the instance isn't running yet "
            "or has no SSH endpoint (e.g. jupyter-only runtype)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "instance_id": {
                    "type": "integer",
                    "description": (
                        "Instance/contract id, as returned by "
                        "create_instance (new_contract) or billing_summary."
                    ),
                },
            },
            "required": ["instance_id"],
        },
    ),
    types.Tool(
        name="get_instance_endpoint",
        description=(
            "Get the publicly reachable HTTP URL(s) for a running instance's "
            "exposed container ports (e.g. the port opened via create_instance's "
            "env, like '-p 8000:8000' for a vLLM/API server) - pure API, no "
            "SSH/CLI required. Vast.ai NATs each exposed container port to a "
            "random external port on the host's shared public IP; this "
            "resolves that mapping so the port is directly callable over "
            "HTTP. Pass container_port to get just that one URL. Returns an "
            "error if the instance is still loading or the port isn't "
            "exposed yet (mappings can take a few seconds to appear after "
            "the container starts)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "instance_id": {
                    "type": "integer",
                    "description": (
                        "Instance/contract id, as returned by "
                        "create_instance (new_contract) or billing_summary."
                    ),
                },
                "container_port": {
                    "type": "integer",
                    "description": (
                        "Only return the URL for this container port (e.g. "
                        "8000). Omit to return all exposed ports."
                    ),
                },
            },
            "required": ["instance_id"],
        },
    ),
    types.Tool(
        name="get_instance_logs",
        description=(
            "Fetch recent logs for an instance by its contract/instance id. "
            "By default returns container (docker) logs; set daemon_logs to "
            "fetch the host daemon's system logs instead. Returns the log "
            "text directly (truncated if very large), plus the S3 URL it "
            "came from."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "instance_id": {
                    "type": "integer",
                    "description": (
                        "Instance/contract id, as returned by "
                        "create_instance (new_contract) or billing_summary."
                    ),
                },
                "tail": {
                    "type": "string",
                    "description": "Number of lines from the end of the logs to return, e.g. '200'.",
                },
                "filter_str": {
                    "type": "string",
                    "description": "Grep-style filter applied to log entries.",
                },
                "daemon_logs": {
                    "type": "boolean",
                    "description": "Fetch host daemon system logs instead of container logs (default false).",
                },
            },
            "required": ["instance_id"],
        },
    ),
    types.Tool(
        name="create_api_key",
        description=(
            "Create a new Vast.ai API key on the caller's account. Optionally "
            "scoped down via a permissions object (omit for a full-access "
            "key, matching an unrestricted key created in the web console). "
            "Returns the new key id and the plaintext key value (shown only "
            "once)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Friendly name for the key.",
                },
                "permissions": {
                    "type": "object",
                    "description": (
                        "Optional permissions object restricting the key's "
                        "scope, per Vast.ai's roles-and-permissions format. "
                        "Omit for full account access."
                    ),
                },
            },
            "required": ["name"],
        },
    ),
    types.Tool(
        name="list_api_keys",
        description=(
            "List existing Vast.ai API keys on the caller's account (id, "
            "name, key_type, created_at, deleted_at, etc; the plaintext key "
            "value itself is never returned by this endpoint). Use this "
            "before delete_api_key to see what exists and confirm the "
            "right id."
        ),
        input_schema={"type": "object", "properties": {}},
    ),
    types.Tool(
        name="delete_api_key",
        description=(
            "Revoke (delete) an existing Vast.ai API key by its id, as "
            "returned by create_api_key or list_api_keys. Irreversible; any "
            "client still using that key immediately loses access."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "key_id": {
                    "type": "integer",
                    "description": "API key id to delete, as returned by create_api_key.",
                },
            },
            "required": ["key_id"],
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
    """Rent a new standalone persistent volume, not attached to any instance.

    There is no dedicated volume-search endpoint; standalone volumes are
    rented the same way an instance's on-the-fly volume is: pick a GPU
    offer's local volume slot (avail_vol_ask_id, from /api/v0/bundles/) with
    enough free space, then PUT /api/v0/volumes with that id and the
    requested size.
    """
    offers = _request(
        "POST",
        "/api/v0/bundles/",
        json={
            "limit": 10,
            "type": "on-demand",
            "verified": {"eq": True},
            "rentable": {"eq": True},
            "rented": {"eq": False},
            "external": {"eq": False},
            "avail_vol_size": {"gte": size_gb},
            "order": [["avail_vol_size", "asc"]],
        },
    )
    vol_offers = [o for o in (offers.get("offers") or []) if o.get("avail_vol_ask_id")]
    if not vol_offers:
        return {
            "success": False,
            "error": (
                f"No offer found with a free volume slot >= {size_gb} GB. "
                "Try a smaller size, or attach a volume at instance creation "
                "instead (create_instance with volume={size_gb, mount_path})."
            ),
        }
    offer = vol_offers[0]
    return _request(
        "PUT",
        "/api/v0/volumes",
        json={"id": offer["avail_vol_ask_id"], "size": size_gb},
    )


@tool
def delete_volume(volume_id: int) -> dict[str, Any]:
    """Delete a rented volume by its id (from list_volumes), stopping billing.

    Volumes are deleted through /api/v0/instances/{id}/ when standalone, but
    a volume left over after its parent instance was already destroyed needs
    /api/v0/volumes instead; this tries the former first and falls back.
    """
    try:
        return _request("DELETE", f"/api/v0/instances/{volume_id}/")
    except RuntimeError:
        return _request("DELETE", "/api/v0/volumes", json={"id": volume_id})


@tool
def list_volumes() -> dict[str, Any]:
    """List all rented volumes."""
    return _request("GET", "/api/v0/volumes/")


def _offer_lookup(offer_id: int) -> dict[str, Any] | None:
    """Fetch the live bundle for a single offer, or None if not found."""
    resp = _request(
        "POST",
        "/api/v0/bundles/",
        json={
            "limit": 1,
            "type": "on-demand",
            "verified": {"eq": True},
            "rentable": {"eq": True},
            "rented": {"eq": False},
            "external": {"eq": False},
            "ask_contract_id": {"eq": offer_id},
        },
    )
    offers = resp.get("offers") or []
    return offers[0] if offers else None


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
    onstart: str | None = None,
    args_str: str | None = None,
    volume: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create an instance on the given offer, optionally with a volume.

    Refuses to rent if the offer's live price exceeds max_hourly_price or
    can't be verified, so a caller (human or LLM) always states a spend cap
    up front instead of trusting a possibly-stale or hallucinated offer_id.
    """
    offer = _offer_lookup(offer_id)
    if offer is None:
        return {
            "success": False,
            "error": (
                f"Could not verify the current price of offer {offer_id} "
                "(it may no longer be available). Refusing to rent without "
                "a verified price. Re-run search_offers to get a fresh "
                "offer_id."
            ),
        }
    price = float(offer.get("dph_total") or 0.0)
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
    if onstart:
        body["onstart"] = onstart
    if args_str:
        body["args_str"] = args_str
    if volume:
        vol_info: dict[str, Any] = {"mount_path": volume.get("mount_path", "/data")}
        if volume.get("volume_id"):
            vol_info["create_new"] = False
            vol_info["volume_id"] = volume["volume_id"]
        else:
            avail_vol_ask_id = offer.get("avail_vol_ask_id")
            if not avail_vol_ask_id:
                return {
                    "success": False,
                    "error": (
                        f"Offer {offer_id} has no local volume slot available "
                        "(avail_vol_ask_id missing) to create a new volume "
                        "on. Pass an existing volume_id instead, or pick a "
                        "different offer."
                    ),
                }
            vol_info["create_new"] = True
            vol_info["size"] = volume.get("size_gb", 15)
            vol_info["volume_id"] = avail_vol_ask_id
        body["volume_info"] = vol_info
    return _request("PUT", f"/api/v0/asks/{offer_id}", json=body)


@tool
def destroy_instance(instance_id: int) -> dict[str, Any]:
    """Destroy (terminate) a rented instance, stopping its billing."""
    return _request("DELETE", f"/api/v0/instances/{instance_id}/")


@tool
def list_deleted_instances(limit: int = 50) -> dict[str, Any]:
    """List previously destroyed instances from the account's audit log.

    There's no dedicated Vast.ai endpoint for destroyed instances; once
    destroyed the full instance record (gpu_name, image, etc.) is gone.
    /api/v0/audit_logs/ logs every API call, so instance_DELETE calls give
    us instance ids + timestamps, which we pair with the ask_PUT (create)
    call for the same contract/instance id to recover the rental window.
    """
    logs = _request("GET", "/api/v0/audit_logs/")
    if not isinstance(logs, list):
        return {"success": False, "error": "Unexpected audit_logs response.", "raw": logs}

    logs_sorted = sorted(logs, key=lambda e: e.get("created_at") or 0)
    created_at_by_id: dict[Any, float] = {}
    deletions: list[dict[str, Any]] = []
    for entry in logs_sorted:
        route = entry.get("api_route")
        args = entry.get("args") or {}
        if route == "api.ask_PUT" and "contract_id" in args:
            created_at_by_id[args["contract_id"]] = entry.get("created_at")
        elif route == "api.instance_DELETE" and "instance_id" in args:
            iid = args["instance_id"]
            created_at = created_at_by_id.pop(iid, None)
            deleted_at = entry.get("created_at")
            deletions.append(
                {
                    "instance_id": iid,
                    "created_at": created_at,
                    "deleted_at": deleted_at,
                    "duration_seconds": (
                        round(deleted_at - created_at, 1)
                        if created_at and deleted_at
                        else None
                    ),
                }
            )

    deletions.sort(key=lambda e: e.get("deleted_at") or 0, reverse=True)
    return {"success": True, "deleted_instances": deletions[:limit]}


@tool
def search_base_images(
    query: str | None = None,
    recommended_only: bool | None = None,
    limit: int = 30,
) -> dict[str, Any]:
    """Search available docker image templates usable as create_instance's image."""
    if recommended_only is None:
        recommended_only = not query
    select_filters: dict[str, Any] = {}
    if recommended_only:
        select_filters["recommended"] = {"eq": True}
    params = {
        "select_cols": json.dumps(["id", "name", "image", "tag", "recommended", "use_ssh", "tags"]),
    }
    if select_filters:
        params["select_filters"] = json.dumps(select_filters)
    resp = _request("GET", "/api/v0/template", params=params)
    templates = resp.get("templates") or []
    if query:
        q = query.lower()
        templates = [
            t for t in templates
            if q in (t.get("name") or "").lower() or q in (t.get("image") or "").lower()
        ]
    return {"success": True, "templates": templates[:limit]}


@tool
def get_ssh_connection(instance_id: int) -> dict[str, Any]:
    """Return the SSH command and connection fields for a running instance.

    GET /api/v0/instances/{id}/ returns ssh_host/ssh_port (the SSH forwarder
    address for both ssh_proxy and direct runtypes) alongside actual_status.
    """
    resp = _request("GET", f"/api/v0/instances/{instance_id}/")
    inst = resp.get("instances") or {}
    if not inst:
        return {
            "success": False,
            "error": f"Instance {instance_id} not found.",
        }

    status = inst.get("actual_status")
    ssh_host = inst.get("ssh_host")
    ssh_port = inst.get("ssh_port")

    if not ssh_host or not ssh_port:
        return {
            "success": False,
            "instance_id": instance_id,
            "status": status,
            "status_msg": inst.get("status_msg"),
            "error": (
                "No SSH endpoint available yet. The instance may still be "
                "loading, may not be running, or its runtype may not "
                "expose SSH (e.g. jupyter-only)."
            ),
        }

    return {
        "success": True,
        "instance_id": instance_id,
        "status": status,
        "ssh_host": ssh_host,
        "ssh_port": ssh_port,
        "ssh_command": f"ssh -p {ssh_port} root@{ssh_host}",
    }


@tool
def get_instance_endpoint(
    instance_id: int, container_port: int | None = None
) -> dict[str, Any]:
    """Return publicly reachable http(s) URLs for an instance's exposed ports.

    GET /api/v0/instances/{id}/ carries the port mapping, but Vast.ai's
    "ports" field shape is inconsistent across hosts: sometimes a Docker-style
    dict ({"8000/tcp": [{"HostIp": ..., "HostPort": ...}]}), sometimes a plain
    list of container port ints (host port assumed == container port on
    direct-networking hosts). Both are handled defensively; public_ipaddr is
    used as the reachable host in both cases (ssh_host is for SSH only).
    """
    resp = _request("GET", f"/api/v0/instances/{instance_id}/")
    inst = resp.get("instances") or {}
    if not inst:
        return {"success": False, "error": f"Instance {instance_id} not found."}

    status = inst.get("actual_status")
    public_ip = inst.get("public_ipaddr")
    ports = inst.get("ports")

    endpoints: list[dict[str, Any]] = []
    if isinstance(ports, dict):
        for key, bindings in ports.items():
            c_port_str = key.split("/")[0]
            if not c_port_str.isdigit():
                continue
            c_port = int(c_port_str)
            if container_port is not None and c_port != container_port:
                continue
            for binding in bindings or []:
                host_ip = binding.get("HostIp") or public_ip
                host_port = binding.get("HostPort")
                if not host_ip or not host_port:
                    continue
                endpoints.append(
                    {
                        "container_port": c_port,
                        "host_ip": host_ip,
                        "host_port": int(host_port),
                        "url": f"http://{host_ip}:{host_port}",
                    }
                )
    elif isinstance(ports, list):
        for c_port in ports:
            if container_port is not None and c_port != container_port:
                continue
            if not public_ip:
                continue
            endpoints.append(
                {
                    "container_port": c_port,
                    "host_ip": public_ip,
                    "host_port": c_port,
                    "url": f"http://{public_ip}:{c_port}",
                }
            )

    if not endpoints:
        return {
            "success": False,
            "instance_id": instance_id,
            "status": status,
            "status_msg": inst.get("status_msg"),
            "public_ipaddr": public_ip,
            "raw_ports": ports,
            "error": (
                "No exposed port endpoint found. The instance may still be "
                "loading, may not have been created with a direct-networking "
                "runtype (ssh_direct/jupyter_direct/args), or the requested "
                "container_port isn't exposed."
            ),
        }

    return {
        "success": True,
        "instance_id": instance_id,
        "status": status,
        "endpoints": endpoints,
    }


_MAX_LOG_CHARS = 20000


@tool
def get_instance_logs(
    instance_id: int,
    tail: str | None = None,
    filter_str: str | None = None,
    daemon_logs: bool = False,
) -> dict[str, Any]:
    """Request logs for an instance and fetch their content from the resulting S3 URL.

    PUT /api/v0/instances/request_logs/{id} only returns a presigned S3 URL,
    not the log text itself; this fetches that URL (a different host than
    BASE_URL, so plain httpx.get, not _request) and returns the text.
    """
    body: dict[str, Any] = {}
    if tail is not None:
        body["tail"] = tail
    if filter_str is not None:
        body["filter"] = filter_str
    if daemon_logs:
        body["daemon_logs"] = "true"

    resp = _request("PUT", f"/api/v0/instances/request_logs/{instance_id}/", json=body)
    result_url = resp.get("result_url")
    if not result_url:
        return {
            "success": False,
            "instance_id": instance_id,
            "error": resp.get("msg") or "No result_url returned for logs.",
        }

    # The API returns the S3 url before the log file is actually written
    # there ("...in a few seconds"), so a fresh request commonly 403s once
    # or twice before the object exists.
    log_resp = None
    for attempt in range(5):
        log_resp = httpx.get(result_url, timeout=60.0)
        if log_resp.status_code < 400:
            break
        time.sleep(1.5)
    log_resp.raise_for_status()
    text = log_resp.text
    truncated = len(text) > _MAX_LOG_CHARS
    if truncated:
        text = text[-_MAX_LOG_CHARS:]

    return {
        "success": True,
        "instance_id": instance_id,
        "result_url": result_url,
        "truncated": truncated,
        "logs": text,
    }


@tool
def create_api_key(name: str, permissions: dict[str, Any] | None = None) -> dict[str, Any]:
    """Create a new API key on the caller's account, optionally scoped."""
    body: dict[str, Any] = {"name": name}
    if permissions is not None:
        body["permissions"] = permissions
    return _request("POST", "/api/v0/auth/apikeys", json=body)


@tool
def list_api_keys() -> dict[str, Any]:
    """List all API keys on the caller's account."""
    return _request("GET", "/api/v0/auth/apikeys/")


@tool
def delete_api_key(key_id: int) -> dict[str, Any]:
    """Revoke an API key by id."""
    return _request("DELETE", f"/api/v0/auth/apikeys/{key_id}")


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
