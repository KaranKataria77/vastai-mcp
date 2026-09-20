# AGENTS.md

Vast.ai MCP server: exposes Vast.ai cloud operations (GPU offers, volumes, instances, billing) as MCP tools over two transports, stdio (`vastai_mcp/server.py`) and streamable HTTP (`vastai_mcp/server_http.py`). Packaged as an installable pip package (`pyproject.toml`, package dir `vastai_mcp/`) with console scripts `vastai-mcp` (stdio) and `vastai-mcp-http` (HTTP).

## Commands

- Setup (from source): `pip install -e ".[http]"`
- Setup (from PyPI, once published): `pip install "vastai-mcp[http]"`
- Run stdio: `vastai-mcp` (for MCP clients like Claude Desktop)
- Run HTTP: `vastai-mcp-http` (listens on `MCP_HOST:PORT`, default `0.0.0.0:8000`, endpoints `GET /health`, `POST /mcp`)
- Build distributables: `python -m build` (produces `dist/*.whl` and `dist/*.tar.gz`, both gitignored)
- No test suite, linter, or type checker is configured. Validate manually: `python -c "from vastai_mcp import server"` for imports, or exercise tools via the HTTP endpoint with a real `VAST_API_KEY`.

## Architecture

`vastai_mcp/server.py` is the single source of truth for tool logic:

- `_request(method, path, **kwargs)` (server.py) is the only Vast.ai API call path. It builds a fresh `httpx.Client` against `BASE_URL = https://console.vast.ai` with `Authorization: Bearer <key>` from `_api_key()`, 60s timeout, and raises `RuntimeError` on any HTTP >= 400.
- `_api_key()` resolves the key from a contextvar (`_request_api_key`) first, falling back to the `VAST_API_KEY` env var. The contextvar is set per-request by `server_http.py`'s `PerRequestVastKeyMiddleware` from the caller's `X-Vast-Api-Key` header — this is what makes the HTTP deployment multi-tenant (each caller's own key/billing), while stdio mode keeps using the env var. Never make `_api_key()` read only from `os.environ` again; that would silently revert to single-tenant billing on the operator's account.
- `create_instance` requires `max_hourly_price` and calls `_offer_price(offer_id)` to verify the offer's live `dph_total` before renting, refusing if it exceeds the cap or can't be fetched. This is a deliberate spend guardrail against an LLM caller renting an unexpectedly expensive offer — don't remove it or make `max_hourly_price` optional.
- Tool definitions live in the `TOOLS` list (raw `types.Tool` dicts), and implementations are plain sync functions registered via the `@tool` decorator into the `HANDLERS` dict. Dispatch happens in `on_call_tool`, which calls `handler(**args)` and JSON-encodes the result.
- `server_http.py` does `from . import server as mcp_core` (relative import, both live under the `vastai_mcp` package) and reuses `on_list_tools` / `on_call_tool` directly. It does not duplicate tool logic.
- `server.py:cli()` and `server_http.py:cli()` are the console-script entry points wired in `pyproject.toml`'s `[project.scripts]` — keep their names in sync if renamed.

When adding a tool: add a `types.Tool` entry to `TOOLS`, add a matching `@tool`-decorated function. The function name must equal the tool `name` (dispatch is by name). No other wiring needed.

## Tool-to-API mapping (non-obvious parts)

- `search_offers` → `POST /api/v0/bundles/` with a JSON query body. Filters use operator dicts (`{"eq": ...}`, `{"gte": ...}`, `{"in": [...]}`), not flat query params. `order` is a list of `[field, direction]` pairs.
- `create_instance` → `PUT /api/v0/asks/{offer_id}` (the "ask id" is the offer id returned by search_offers). Volumes are attached via a `volume_info` object: `create_new: true` + `size` to create on the fly, or `create_new: false` + `volume_id` to attach existing.
- `create_volume` is synthesized: the API has no standalone create endpoint. It searches `POST /api/v0/search/volumes/` then rents via `PUT /api/v0/volumes` (which is normally the resize endpoint). Fails with a guidance message if no matching offer exists.
- `list_gpus` → `GET /api/v0/metrics/gpu/current/`. Requires the `machine_read` permission group on the API key; the handler special-cases the error string and returns a hint suggesting `search_offers` instead.
- `billing_summary` combines `GET /api/v0/instances/` with `GET /api/v1/invoices` (note v1, not v0; `select_filters` is a JSON-encoded string param, capped at 200).

## Gotchas

- **`mcp.json` previously contained a real, committed API key — it was rotated/revoked and must never be repopulated with a real key.** Treat any key-shaped string in this repo as compromised; never commit a live key again. The stdio config example belongs in the README, not in a tracked file with a real secret.
- `uvicorn` and `starlette` are declared under the `http` optional-dependency extra in `pyproject.toml` (`pip install "vastai-mcp[http]"`), not in the base dependencies — `server_http.py` imports both and the `mcp` package's streamable-HTTP manager requires them, but stdio-only users shouldn't need them. `requirements.txt` still lists them unconditionally for the plain `pip install -r requirements.txt` / non-package workflow.
- `server_http.py`'s `/mcp` route is auth-gated by `PerRequestVastKeyMiddleware`, which 401s any request missing `X-Api-Key`. `/health` is intentionally left open. When adding routes under `/mcp`, remember the middleware only inspects `scope["path"]` — don't mount unauthenticated tool-invoking routes under that prefix. `X-Api-Key` (not a Vast.ai-specific name) is used deliberately: it's on Claude's remote-MCP connector's pre-approved request-header allowlist, so users adding this server as a custom connector don't need Anthropic support to whitelist a custom header name.
- The top-level `server = Server(...)` is dead code (never used); the real server is built inside `main()`.
- The `create_volume` docstring is partially stale/aspirational (it describes a "search + ask flow" that isn't what the code does). Trust the code.
- Tool handlers are synchronous and make blocking network calls inside the async MCP dispatch loop. This is fine for low concurrency but don't assume async-safety when adding tools.
- `on_call_tool` catches all exceptions and returns them as `is_error=True` text results, so Vast.ai API errors surface to the client as tool results, not as MCP protocol errors.
- API key permission groups vary: `machine_read` gates `list_gpus`; invoice access gates part of `billing_summary` (which degrades gracefully). When testing a new tool, a permission error doesn't necessarily mean the code is wrong.
- Vast.ai response field names are inconsistent (e.g. cost fields live under `instance`, `search`, or the top level); `billing_summary` falls back through several field names. Follow the same defensive pattern when reading API responses.

## Conventions

- Python 3.10+ (uses `str | None` unions, `from __future__ import annotations`).
- Type hints on all functions; tool handlers return `dict[str, Any]`.
- No tests, no lint config, no CI.
