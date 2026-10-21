"""stdio MCP server: protocol on stdout only, logs on stderr, no prompts, clean EOF exit."""

from __future__ import annotations

import json
import logging
import sys
from importlib import metadata
from typing import Any

import anyio
import httpx
from mcp import types
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.shared.exceptions import MCPError
from pydantic import BaseModel, ValidationError

from sbx.config import ClientConfig
from sbx.mcp_server.errors import from_sbx, from_validation, tool_error
from sbx.mcp_server.tools import BY_NAME, TOOLS, OutputTooLarge, ToolSpec
from sbx.sdk import SBXClient, SBXError

SERVER_NAME = "sbx"
log = logging.getLogger("sbx.mcp")

INSTRUCTIONS = (
    "SBX runs durable coding-agent Sessions. Start with sbx_list_resources to see which "
    "harness and resource combinations are usable, then sbx_create_task with an explicit "
    "inference_mode and resource id. Creating, following up, cancelling and retrying can run "
    "commands or spend resources. Tool calls return ids at once; poll sbx_get_task and "
    "sbx_get_result for the outcome. Reuse an idempotency_key only to retry the same request."
)


def _version() -> str:
    try:
        return metadata.version("sbx-agent")
    except metadata.PackageNotFoundError:
        return "0"


def _client() -> SBXClient:
    """A fresh client per call: a revoked key is rejected on the next call, not cached."""
    try:
        cfg = ClientConfig.load()
    except ValueError:
        raise SBXError(
            "config_invalid", "the client configuration file is not valid JSON"
        ) from None
    if not cfg.api_key:
        raise SBXError(
            "unauthenticated",
            "no API key is configured",
            details={"source": "client_config"},
        )
    return SBXClient(cfg.base_url, cfg.api_key, workspace_id=cfg.workspace_id)


def _run(spec: ToolSpec, args: BaseModel) -> types.CallToolResult:
    client: SBXClient | None = None
    try:
        client = _client()
        payload = spec.handler(client, args)
    except SBXError as exc:
        log.warning("%s failed: %s", spec.name, exc.code)
        return from_sbx(exc)
    except OutputTooLarge as exc:
        return tool_error("result_too_large", str(exc))
    except httpx.HTTPError:  # the SDK maps transport failures; this guards anything else
        log.warning("%s failed: transport", spec.name)
        return tool_error("transport_error", "the SBX API could not be reached", retryable=True)
    except Exception as exc:  # never leak internals or argument values to the client
        log.error("%s failed unexpectedly: %s", spec.name, type(exc).__name__)
        return tool_error("internal_error", "unexpected failure; see the server log")
    finally:
        if client is not None:
            client.http.close()
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=json.dumps(payload, default=str))],
        structured_content=payload,
        is_error=False,
    )


async def list_tools(_ctx: Any, _params: Any) -> types.ListToolsResult:
    return types.ListToolsResult(tools=[spec.describe() for spec in TOOLS], cache_scope="private")


async def call_tool(_ctx: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
    spec = BY_NAME.get(params.name)
    if spec is None:
        raise MCPError(-32602, f"unknown tool: {params.name}")
    try:
        args = spec.args.model_validate(params.arguments or {})
    except ValidationError as exc:
        return from_validation(exc)
    return await anyio.to_thread.run_sync(_run, spec, args)


def build_server() -> Server:
    return Server(
        SERVER_NAME,
        version=_version(),
        instructions=INSTRUCTIONS,
        on_list_tools=list_tools,
        on_call_tool=call_tool,
    )


def _rpc(message: Any) -> Any:
    return getattr(message.message, "root", message.message)


async def _inbound(source: Any, sink: Any, owed: set[Any]) -> None:
    """Forward requests and remember their ids; at EOF wait until every owed answer is written."""
    async with sink:
        async with source:
            async for message in source:
                rpc = _rpc(message)
                if (
                    getattr(rpc, "method", None) is not None
                    and getattr(rpc, "id", None) is not None
                ):
                    owed.add(rpc.id)
                await sink.send(message)
        while owed:
            await anyio.sleep(0.02)


async def _outbound(source: Any, sink: Any, owed: set[Any]) -> None:
    """Write to stdout; a response retires its request from the owed set once written."""
    async with sink:
        async for message in source:
            await sink.send(message)
            rpc = _rpc(message)
            if getattr(rpc, "method", None) is None and getattr(rpc, "id", None) is not None:
                owed.discard(rpc.id)


async def _serve() -> None:
    server = build_server()
    owed: set[Any] = set()
    async with stdio_server() as (read, write):
        to_server, server_in = anyio.create_memory_object_stream[Any](0)
        server_out, from_server = anyio.create_memory_object_stream[Any](0)
        async with anyio.create_task_group() as tg:
            tg.start_soon(_inbound, read, to_server, owed)
            tg.start_soon(_outbound, from_server, write, owed)
            await server.run(server_in, server_out, server.create_initialization_options())
            await server_out.aclose()


def serve() -> int:
    # Diagnostics only on stderr; stdout carries JSON-RPC frames and nothing else.
    logging.basicConfig(
        stream=sys.stderr, level=logging.WARNING, format="sbx-mcp %(levelname)s: %(message)s"
    )
    anyio.run(_serve)
    return 0
