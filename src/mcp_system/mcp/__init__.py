"""Transport-independent MCP tools and JSON-RPC protocol server."""

from .dispatcher import (
    MCPDispatcher,
    MCPProtocolError,
    SurfaceRegistry,
    SurfaceSpec,
    ToolSpec,
)
from .protocol import MCPJSONRPCServer, run_stdio
from .launcher import build_client_config

__all__ = [
    "MCPDispatcher",
    "MCPJSONRPCServer",
    "MCPProtocolError",
    "SurfaceRegistry",
    "SurfaceSpec",
    "ToolSpec",
    "build_client_config",
    "run_stdio",
]
