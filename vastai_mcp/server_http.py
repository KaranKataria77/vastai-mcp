"""Vast.ai MCP server - HTTP transport for web deployment.

Run:  vastai-mcp-http   (console script; or `python -m vastai_mcp.server_http`)
Env:  VAST_API_KEY=<your key>  (fallback only — HTTP callers should send
      X-Vast-Api-Key per request instead, see below)
      MCP_HOST=0.0.0.0  (default)
      MCP_PORT=8000     (default)

Endpoints:
  GET  /health  -> liveness check
  POST /mcp     -> MCP streamable-HTTP endpoint (JSON-RPC, JSON responses)

Multi-tenant auth: this server holds no Vast.ai key of its own. Every /mcp
request must carry the caller's own key in the `X-Vast-Api-Key` header; that
key is used for that request only, so billing/rentals happen against the
caller's Vast.ai account, not the operator's. Requests without the header are
rejected with 401 before they reach the MCP layer.

Any MCP-compatible web client can point at https://<host>:<port>/mcp with
that header set.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager

import uvicorn
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import (
    StreamableHTTPASGIApp,
    StreamableHTTPSessionManager,
)
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route
from starlette.types import ASGIApp, Receive, Scope, Send

from . import server as mcp_core  # reuses on_list_tools / on_call_tool / _request_api_key

HOST = os.environ.get("MCP_HOST", "0.0.0.0")
PORT = int(os.environ.get("MCP_PORT", "8000"))
if os.environ.get("MCP_FORCE_PORT"):
    PORT = int(os.environ["MCP_FORCE_PORT"])

mcp = Server(
    "vastai-mcp",
    on_list_tools=mcp_core.on_list_tools,
    on_call_tool=mcp_core.on_call_tool,
)

session_manager = StreamableHTTPSessionManager(
    app=mcp,
    stateless=True,
    json_response=True,
)

mcp_asgi = StreamableHTTPASGIApp(session_manager)


class PerRequestVastKeyMiddleware:
    """Gate /mcp on a caller-supplied X-Vast-Api-Key header.

    Sets it as a contextvar for the duration of the request so server.py's
    _api_key() picks it up instead of any server-side env var, keeping each
    caller's Vast.ai billing/rentals on their own account. Plain ASGI (not
    BaseHTTPMiddleware) so streaming MCP responses aren't buffered.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope["path"].startswith("/mcp"):
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers") or [])
        raw_key = headers.get(b"x-vast-api-key")
        if not raw_key or not raw_key.decode().strip():
            response = JSONResponse(
                {
                    "error": (
                        "Missing X-Vast-Api-Key header. Every request must "
                        "include your own Vast.ai API key so rentals and "
                        "billing apply to your account, not the server "
                        "operator's."
                    )
                },
                status_code=401,
            )
            await response(scope, receive, send)
            return

        token = mcp_core._request_api_key.set(raw_key.decode().strip())
        try:
            await self.app(scope, receive, send)
        finally:
            mcp_core._request_api_key.reset(token)


@asynccontextmanager
async def lifespan(app: Starlette):
    async with session_manager.run():
        yield


app = Starlette(
    routes=[
        Route(
            "/health",
            lambda r: JSONResponse(
                {"status": "ok", "server": "vastai-mcp", "endpoint": "/mcp"}
            ),
            methods=["GET"],
        ),
        Mount("/mcp", app=mcp_asgi),
    ],
    lifespan=lifespan,
)
app = PerRequestVastKeyMiddleware(app)

def cli() -> None:
    """Synchronous entry point for the `vastai-mcp-http` console script."""
    import argparse

    from . import __version__

    parser = argparse.ArgumentParser(prog="vastai-mcp-http")
    parser.add_argument("--version", action="version", version=__version__)
    parser.parse_args()

    uvicorn.run(app, host=HOST, port=PORT, log_level="info")


if __name__ == "__main__":
    cli()
